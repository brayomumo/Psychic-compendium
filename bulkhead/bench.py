"""Benchmark: how dependency A fares under load while dependency B degrades.

Requests arrive at a fixed rate, each going to A or B at random (seeded).
Every request has a client deadline. A request answers "in time" if it
completed OK within that deadline, counted from when it was sent, so queueing
for a request worker counts against it, as it would for a real client.

Each variant runs ``--repetitions`` times. The report gives the median and
the min-max range of each metric, with the environment, so results can be
reproduced and compared.
"""

import argparse
import math
import os
import platform
import random
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, wait
from dataclasses import dataclass

from _cli import (
    EXIT_OK,
    bounded_float,
    bounded_int,
    configure_logging,
    run_main,
)
from service import (
    Dependency,
    Mode,
    Outcome,
    Result,
    Service,
    ServiceConfig,
    Variant,
)

__all__ = ["RunMetrics", "main", "percentile", "run_once"]

NO_ANSWER = "no answer"  # Missed the client deadline, or never finished.
B_COLUMNS = (*(o.value for o in Outcome), NO_ANSWER)


@dataclass(frozen=True)
class RunMetrics:
    """What one run measured.

    Attributes:
        a_sent: Requests sent to A.
        a_in_time: Fraction of A's requests answered OK within the deadline.
        a_p50_ms: Median latency of A's in-time answers, if any.
        a_p99_ms: 99th-percentile latency of A's in-time answers, if any.
        b_share: For each B column, its fraction of B's requests.
        offered_rate: Requests per second actually sent.
    """

    a_sent: int
    a_in_time: float
    a_p50_ms: float | None
    a_p99_ms: float | None
    b_share: dict[str, float]
    offered_rate: float


def percentile(values: Sequence[float], pct: float) -> float:
    """Returns the nearest-rank percentile of ``values``.

    Raises:
        ValueError: If ``values`` is empty or ``pct`` is outside (0, 100].
    """
    if not values:
        raise ValueError("percentile of no values")
    if not 0 < pct <= 100:  # noqa: PLR2004 - percentages.
        raise ValueError(f"pct must be in (0, 100], got {pct}")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _degrade(dependency: Dependency, mode: Mode, latency: float) -> None:
    if mode is Mode.HUNG:
        dependency.hang()
    elif mode is Mode.SLOW:
        dependency.slow(latency)
    elif mode is Mode.FAILING:
        dependency.fail()


def _classify(
    sent: Sequence[tuple[str, Future[Result]]], deadline: float
) -> tuple[list[float], int, dict[str, int]]:
    a_latencies: list[float] = []
    a_sent = 0
    b_counts = dict.fromkeys(B_COLUMNS, 0)
    for name, future in sent:
        result = future.result() if future.done() else None
        answered = (
            result
            if result is not None and result.total_s <= deadline
            else None
        )
        if name == "A":
            a_sent += 1
            if answered is not None and answered.outcome is Outcome.OK:
                a_latencies.append(answered.total_s * 1000)
        elif answered is not None:
            b_counts[answered.outcome.value] += 1
        else:
            b_counts[NO_ANSWER] += 1
    return a_latencies, a_sent, b_counts


def run_once(
    variant: Variant, args: argparse.Namespace, seed: int
) -> RunMetrics:
    """Drives one variant with the configured load and measures it.

    Args:
        variant: The isolation variant.
        args: Parsed command-line options.
        seed: Seeds the A/B choice of each request.

    Returns:
        The run's metrics.
    """
    healthy = args.a_latency_ms / 1000
    a, b = Dependency("A", healthy), Dependency("B", healthy)
    _degrade(b, Mode(args.b_mode), args.b_latency_ms / 1000)
    config = ServiceConfig(
        workers=args.workers,
        limit=args.limit,
        queue_size=args.queue,
        timeout=args.timeout_ms / 1000,
    )
    rng = random.Random(seed)
    count = max(1, round(args.duration * args.rate))
    interval = 1 / args.rate
    deadline = args.client_timeout_ms / 1000
    sent: list[tuple[str, Future[Result]]] = []
    service = Service(variant, {"A": a, "B": b}, config)
    try:
        start = time.perf_counter()
        for i in range(count):
            delay = start + i * interval - time.perf_counter()
            if delay > 0:
                time.sleep(delay)  # Paces arrivals; simulates the clients.
            name = "A" if rng.random() < args.a_share else "B"
            sent.append((name, service.submit(name)))
        elapsed = max(time.perf_counter() - start, 1e-9)
        # Give the last request its full deadline; stop early if all are in.
        wait([f for _, f in sent], timeout=deadline)
        a_latencies, a_sent, b_counts = _classify(sent, deadline)
    finally:
        a.shutdown()
        b.shutdown()
        service.close()
    b_sent = sum(b_counts.values())
    return RunMetrics(
        a_sent=a_sent,
        a_in_time=len(a_latencies) / a_sent if a_sent else math.nan,
        a_p50_ms=percentile(a_latencies, 50) if a_latencies else None,
        a_p99_ms=percentile(a_latencies, 99) if a_latencies else None,
        b_share={
            k: v / b_sent if b_sent else math.nan for k, v in b_counts.items()
        },
        offered_rate=count / elapsed,
    )


def _spread(values: Sequence[float | None], fmt: Callable[[float], str]) -> str:
    present = [v for v in values if v is not None and not math.isnan(v)]
    if not present:
        return "-"
    median = fmt(statistics.median(present))
    low, high = fmt(min(present)), fmt(max(present))
    return median if low == high else f"{median} ({low}-{high})"


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _ms(value: float) -> str:
    return f"{value:.1f} ms"


def _report(
    args: argparse.Namespace, results: dict[Variant, list[RunMetrics]]
) -> None:
    b_load = {
        "hung": "B hung",
        "slow": f"B slow ({args.b_latency_ms} ms)",
        "failing": "B failing fast",
        "healthy": "B healthy",
    }[args.b_mode]
    print(
        f"environment: {platform.platform()} · {os.cpu_count()} CPUs · "
        f"Python {platform.python_version()}\n"
        f"load: {args.rate} req/s for {args.duration:g} s, "
        f"{args.a_share:.0%} to A (latency {args.a_latency_ms} ms); "
        f"{b_load}\n"
        f"service: {args.workers} request workers, bulkhead capacity "
        f"{args.limit} per dependency (queue {args.queue}), thread-pool "
        f"timeout {args.timeout_ms} ms; client deadline "
        f"{args.client_timeout_ms} ms\n"
        f"repetitions: {args.repetitions}; median (min-max); seed {args.seed}"
        "\n"
    )
    print(
        "| variant | A answered in time | A p50 | A p99 | "
        + " | ".join(f"B {c}" for c in B_COLUMNS)
        + " |"
    )
    print("|---|" + "---:|" * (3 + len(B_COLUMNS)))
    for variant, runs in results.items():
        cells = [
            _spread([r.a_in_time for r in runs], _pct),
            _spread([r.a_p50_ms for r in runs], _ms),
            _spread([r.a_p99_ms for r in runs], _ms),
            *(_spread([r.b_share[c] for r in runs], _pct) for c in B_COLUMNS),
        ]
        print(f"| {variant} | " + " | ".join(cells) + " |")
    slowest = min(r.offered_rate for runs in results.values() for r in runs)
    if slowest < 0.95 * args.rate:
        print(
            f"\nwarning: the load generator only reached {slowest:.0f} req/s",
            file=sys.stderr,
        )


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bench.py",
        description="Measure dependency A's availability while B degrades.",
    )
    add = p.add_argument
    add(
        "--duration",
        type=bounded_float(0.1, 3600),
        default=1.0,
        metavar="S",
        help="seconds of load per run (default: %(default)s)",
    )
    add(
        "--rate",
        type=bounded_int(1, 10_000),
        default=200,
        metavar="N",
        help="requests per second (default: %(default)s)",
    )
    add(
        "--a-share",
        type=bounded_float(0, 1),
        default=0.5,
        metavar="F",
        help="fraction of requests sent to A (default: %(default)s)",
    )
    add(
        "--workers",
        type=bounded_int(1, 512),
        default=16,
        metavar="N",
        help="request workers (default: %(default)s)",
    )
    add(
        "--limit",
        type=bounded_int(1, 512),
        default=4,
        metavar="N",
        help="bulkhead capacity per dependency (default: %(default)s)",
    )
    add(
        "--queue",
        type=bounded_int(0, 10_000),
        default=0,
        metavar="N",
        help="thread-pool bulkhead queue size (default: %(default)s)",
    )
    add(
        "--timeout-ms",
        type=bounded_int(1, 60_000),
        default=100,
        metavar="MS",
        help="thread-pool bulkhead call timeout (default: %(default)s)",
    )
    add(
        "--client-timeout-ms",
        type=bounded_int(1, 600_000),
        default=500,
        metavar="MS",
        help="client deadline per request (default: %(default)s)",
    )
    add(
        "--a-latency-ms",
        type=bounded_int(0, 60_000),
        default=5,
        metavar="MS",
        help="healthy call latency (default: %(default)s)",
    )
    add(
        "--b-mode",
        choices=[m.value for m in Mode],
        default="hung",
        help="how B degrades (default: %(default)s)",
    )
    add(
        "--b-latency-ms",
        type=bounded_int(0, 600_000),
        default=1000,
        metavar="MS",
        help="B's latency when slow (default: %(default)s)",
    )
    add(
        "--repetitions",
        type=bounded_int(1, 100),
        default=5,
        metavar="N",
        help="runs per variant (default: %(default)s)",
    )
    add(
        "--seed",
        type=bounded_int(0, 2**32 - 1),
        default=1,
        metavar="N",
        help="seed for the A/B choice (default: %(default)s)",
    )
    add(
        "--quick",
        action="store_true",
        help="a short smoke run (0.5 s, 100 req/s, 1 repetition)",
    )
    add(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log more (-v info, -vv debug)",
    )
    args = p.parse_args(argv)
    if args.quick:
        args.duration, args.rate, args.repetitions = 0.5, 100, 1
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Runs every variant and prints a Markdown table of the results.

    Returns:
        0 on completion, 130 or 143 if interrupted.
    """
    args = _parse(argv)
    configure_logging(args.verbose)

    def body() -> int:
        # The readiness banner: signal handlers are installed by now.
        print(
            f"running: {len(Variant)} variants x {args.repetitions} "
            f"repetitions of {args.duration:g} s",
            flush=True,
        )
        results = {
            variant: [
                run_once(variant, args, args.seed + i)
                for i in range(args.repetitions)
            ]
            for variant in Variant
        }
        _report(args, results)
        return EXIT_OK

    return run_main(body)


if __name__ == "__main__":
    raise SystemExit(main())
