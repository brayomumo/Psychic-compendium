"""Demo: producer processes stream records over pipes to a thread pool.

Run ``python3 demo.py --help`` for the options. ``--inject`` makes producer
``p0`` (or the handler) fail in a chosen way, to watch each failure mode.
"""

from __future__ import annotations

import argparse
import functools
import logging
import multiprocessing
import sys
import time
from collections.abc import Callable, Sequence

import _cli
from piper import (
    ProducerStartError,
    RunReport,
    Source,
    Status,
    describe_exit,
    run_pipeline,
)
from workloads import FAILURES, check_record, records, records_then

LOG = logging.getLogger("demo")
#: Producer failures from workloads.FAILURES, plus handler errors.
INJECTIONS = ("none", *FAILURES, "worker-error")
#: With --inject worker-error, the handler rejects every Nth record.
REJECT_EVERY = 10


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parses and validates the command line.

    Args:
        argv: Arguments without the program name; ``None`` for sys.argv.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Fan records in from producer processes over pipes.",
    )
    parser.add_argument(
        "--producers",
        type=_cli.non_negative_int,
        default=4,
        metavar="N",
        help="producer processes (default: %(default)s)",
    )
    parser.add_argument(
        "--items",
        type=_cli.non_negative_int,
        default=250,
        metavar="N",
        help="records per producer (default: %(default)s)",
    )
    parser.add_argument(
        "--workers",
        type=_cli.positive_int,
        default=4,
        metavar="N",
        help="handler threads in this process (default: %(default)s)",
    )
    parser.add_argument(
        "--max-in-flight",
        type=_cli.positive_int,
        metavar="N",
        help="items queued or running at once; unset means 2 x workers",
    )
    parser.add_argument(
        "--batch-size",
        type=_cli.positive_int,
        default=32,
        metavar="N",
        help="items per pipe message (default: %(default)s)",
    )
    parser.add_argument(
        "--start-method",
        choices=multiprocessing.get_all_start_methods(),
        help="how to start producers; unset means the platform default",
    )
    parser.add_argument(
        "--produce-ms",
        type=_cli.non_negative_float,
        default=0.0,
        metavar="MS",
        help="simulated work per record, producer (default: %(default)s)",
    )
    parser.add_argument(
        "--work-ms",
        type=_cli.non_negative_float,
        default=0.0,
        metavar="MS",
        help="simulated work per record, handler (default: %(default)s)",
    )
    parser.add_argument(
        "--inject",
        choices=INJECTIONS,
        default="none",
        help="make producer p0, or the handler, fail (default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v for progress, -vv for debug (default: %(default)s)",
    )
    return parser.parse_args(argv)


def build_producers(args: argparse.Namespace) -> dict[str, Source[object]]:
    """Returns one picklable source per producer, honouring ``--inject``."""
    producers: dict[str, Source[object]] = {
        f"p{i}": functools.partial(
            records, f"p{i}", args.items, args.produce_ms / 1000
        )
        for i in range(args.producers)
    }
    if args.inject in FAILURES and producers:
        producers["p0"] = functools.partial(
            records_then, args.inject, "p0", args.items // 2
        )
    return producers


def build_handler(args: argparse.Namespace) -> Callable[[object], None]:
    """Returns the per-item handler.

    It runs in this process, so a closure is fine: the handler is never
    pickled. That is the place for anything that cannot cross a process
    boundary.
    """
    work_s = args.work_ms / 1000
    reject = args.inject == "worker-error"

    def handle(item: object) -> None:
        record = check_record(item)
        if work_s:
            time.sleep(work_s)
        if reject and record.index % REJECT_EVERY == 0:
            raise ValueError(f"rejected {record.source}#{record.index}")

    return handle


def print_report(
    report: RunReport, args: argparse.Namespace, elapsed_s: float
) -> None:
    """Writes the result table to stdout and failure details to stderr."""
    print(f"{'producer':<10}{'status':<9}{'items':>7}  exit")
    for p in report.producers:
        exit_text = describe_exit(p.exitcode)
        print(f"{p.name:<10}{p.status:<9}{p.items:>7}  {exit_text}")
    print(
        f"received {report.received}, processed {report.processed}, "
        f"handler errors {report.worker_failures} in {elapsed_s:.2f} s "
        f"({args.start_method or multiprocessing.get_start_method()}, "
        f"{args.workers} workers, batch {args.batch_size})"
    )
    for p in report.producers:
        if p.status is not Status.OK:
            LOG.error("producer %s %s: %s", p.name, p.status, p.detail.strip())
    for error in report.worker_errors:
        LOG.error("handler error: %s", error)
    if report.worker_failures > len(report.worker_errors):
        hidden = report.worker_failures - len(report.worker_errors)
        LOG.error("... and %d more handler errors", hidden)


def _run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    try:
        report = run_pipeline(
            build_producers(args),
            build_handler(args),
            workers=args.workers,
            max_in_flight=args.max_in_flight,
            batch_size=args.batch_size,
            start_method=args.start_method,
        )
    except ProducerStartError as exc:
        LOG.error("%s", exc)
        return _cli.EXIT_FAILURE
    print_report(report, args, time.perf_counter() - started)
    return _cli.EXIT_OK if report.ok else _cli.EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the demo.

    Args:
        argv: Arguments without the program name; ``None`` for sys.argv.

    Returns:
        0 if every record was handled; 1 if anything failed; 2 for bad
        arguments; 130 or 143 after a clean shutdown on SIGINT or SIGTERM.
    """
    args = parse_args(argv)
    _cli.configure_logging(args.verbose)
    return _cli.run_main(lambda: _run(args))


if __name__ == "__main__":
    sys.exit(main())
