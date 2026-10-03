"""Demo: one hung dependency, with and without bulkheads.

Dependency B hangs. The service sends B a burst of requests, then sends a few
to the healthy dependency A, and reports what each variant did with them.
After every scenario B is released, the service is closed, and the demo
checks that every thread it started has been joined.
"""

import argparse
import dataclasses
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, wait
from dataclasses import dataclass

from _cli import (
    EXIT_FAILURE,
    EXIT_OK,
    bounded_int,
    configure_logging,
    run_main,
)
from service import (
    Dependency,
    Outcome,
    Result,
    Service,
    ServiceConfig,
    Variant,
)

__all__ = ["Row", "main", "run_scenario"]


@dataclass(frozen=True)
class Row:
    """What one variant did with the burst.

    Attributes:
        variant: The isolation variant.
        a_answered: Requests to A answered OK within the window.
        a_sent: Requests sent to A.
        b_rejected: Requests to B refused by a full bulkhead.
        b_timed_out: Requests to B abandoned after the bulkhead timeout.
        request_workers_stuck: Request workers still blocked inside B.
        pool_workers_stuck: Bulkhead pool workers still blocked inside B.
        threads_leaked: Threads alive after shutdown that were not before.
    """

    variant: Variant
    a_answered: int
    a_sent: int
    b_rejected: int
    b_timed_out: int
    request_workers_stuck: int
    pool_workers_stuck: int
    threads_leaked: int


def _done_results(futures: Sequence[Future[Result]]) -> list[Result]:
    return [f.result() for f in futures if f.done()]


def run_scenario(
    variant: Variant,
    config: ServiceConfig,
    *,
    b_requests: int,
    a_requests: int,
    window: float,
) -> Row:
    """Hangs B, sends the burst, observes for ``window`` seconds, cleans up.

    Args:
        variant: The isolation variant to run.
        config: Service sizes and timeouts.
        b_requests: Requests sent to the hung dependency B, first.
        a_requests: Requests sent to the healthy dependency A, after.
        window: Seconds to wait for answers before taking the snapshot.

    Returns:
        The observations for this variant.
    """
    threads_before = threading.active_count()
    a, b = Dependency("A"), Dependency("B")
    b.hang()
    service = Service(variant, {"A": a, "B": b}, config)
    try:
        deadline = time.perf_counter() + window
        b_futures = [service.submit("B") for _ in range(b_requests)]
        a_futures = [service.submit("A") for _ in range(a_requests)]
        wait(a_futures, timeout=max(0.0, deadline - time.perf_counter()))
        # Requests that can finish (rejections, timeouts) do so well inside
        # the window; the rest are stuck in B for as long as it hangs.
        wait(b_futures, timeout=max(0.0, deadline - time.perf_counter()))
        a_results = _done_results(a_futures)
        b_results = _done_results(b_futures)
        request_stuck = sum(f.running() for f in b_futures)
        row = Row(
            variant=variant,
            a_answered=sum(r.outcome is Outcome.OK for r in a_results),
            a_sent=a_requests,
            b_rejected=sum(r.outcome is Outcome.REJECTED for r in b_results),
            b_timed_out=sum(r.outcome is Outcome.TIMED_OUT for r in b_results),
            request_workers_stuck=request_stuck,
            pool_workers_stuck=b.stats().in_flight - request_stuck,
            threads_leaked=0,
        )
    finally:
        # In production nothing can "release" a remote service: the client's
        # own I/O timeout has to end the call. The simulation can.
        a.shutdown()
        b.shutdown()
        service.close()
    leaked = threading.active_count() - threads_before
    return dataclasses.replace(row, threads_leaked=leaked)


def _print_report(rows: Sequence[Row], config: ServiceConfig) -> None:
    print(
        f"{'variant':<13}{'A answered':<13}{'B rejected':<12}"
        f"{'B timed out':<13}{'request workers stuck':<23}"
        "pool workers stuck"
    )
    for row in rows:
        answered = f"{row.a_answered} of {row.a_sent}"
        stuck = f"{row.request_workers_stuck} of {config.workers}"
        print(
            f"{row.variant:<13}{answered:<13}{row.b_rejected:<12}"
            f"{row.b_timed_out:<13}{stuck:<23}{row.pool_workers_stuck}"
        )


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Show what a hung dependency does to a service with "
        "and without bulkheads.",
    )
    parser.add_argument(
        "--workers",
        type=bounded_int(1, 256),
        default=4,
        metavar="N",
        help="request workers shared by all dependencies (default: "
        "%(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=bounded_int(1, 256),
        default=2,
        metavar="N",
        help="bulkhead capacity per dependency (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=bounded_int(1, 60_000),
        default=100,
        metavar="MS",
        help="thread-pool bulkhead call timeout (default: %(default)s)",
    )
    parser.add_argument(
        "--window-ms",
        type=bounded_int(10, 60_000),
        default=500,
        metavar="MS",
        help="how long to wait for answers (default: %(default)s)",
    )
    parser.add_argument(
        "--b-requests",
        type=bounded_int(0, 10_000),
        default=8,
        metavar="N",
        help="requests sent to the hung dependency B (default: %(default)s)",
    )
    parser.add_argument(
        "--a-requests",
        type=bounded_int(0, 10_000),
        default=4,
        metavar="N",
        help="requests sent to the healthy dependency A afterwards "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log more (-v info, -vv debug)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the demo for every variant and prints the comparison.

    Returns:
        0, or 1 if any variant left threads behind after shutdown.
    """
    args = _parse(argv)
    configure_logging(args.verbose)
    config = ServiceConfig(
        workers=args.workers,
        limit=args.limit,
        timeout=args.timeout_ms / 1000,
    )

    def body() -> int:
        print(
            f"B hangs. The service sends {args.b_requests} requests to B, "
            f"then {args.a_requests} to A, and waits {args.window_ms} ms.\n"
            f"{config.workers} request workers; bulkhead capacity "
            f"{config.limit} per dependency; thread-pool timeout "
            f"{args.timeout_ms} ms.\n"
        )
        rows = [
            run_scenario(
                variant,
                config,
                b_requests=args.b_requests,
                a_requests=args.a_requests,
                window=args.window_ms / 1000,
            )
            for variant in Variant
        ]
        _print_report(rows, config)
        leaks = [row for row in rows if row.threads_leaked]
        if leaks:
            for row in leaks:
                print(
                    f"{row.variant}: {row.threads_leaked} threads still alive "
                    "after shutdown",
                    file=sys.stderr,
                )
            return EXIT_FAILURE
        print(
            "\nAfter B was released, every variant shut down and joined all "
            "of its threads."
        )
        return EXIT_OK

    return run_main(body)


if __name__ == "__main__":
    raise SystemExit(main())
