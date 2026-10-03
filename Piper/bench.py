"""Is crossing a process boundary worth its cost? Measure, don't guess.

Times identical workloads through ``baseline.run_in_process`` (one process,
items passed by reference) and ``piper.run_pipeline`` (producer processes,
items pickled through pipes). Each run is timed end to end, from start until
every item has been handled, including process start-up, and is checked for
correctness before its time counts. Nothing is printed in the timed region.

Three workloads bracket the trade-off:

* startup: no items at all, so only the fixed cost of a run shows.
* transport: producing an item costs nothing, so only the overhead shows.
* cpu: producing an item costs pure-Python arithmetic, which holds the GIL;
  only separate processes can do it in parallel.
"""

from __future__ import annotations

import argparse
import functools
import logging
import multiprocessing
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import _cli
from baseline import run_in_process
from piper import RunReport, Source, run_pipeline
from workloads import cpu_numbers, noop, numbers

LOG = logging.getLogger("bench")


class BenchmarkError(RuntimeError):
    """A benchmarked run produced a wrong result, so its time is void."""


@dataclass(frozen=True)
class Case:
    """One row of the results table.

    Attributes:
        workload: Which workload this row belongs to.
        mode: How the items travel.
        run: Performs one complete run and returns its report.
        expected: Items the run must process to count.
    """

    workload: str
    mode: str
    run: Callable[[], RunReport]
    expected: int


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parses and validates the command line.

    Args:
        argv: Arguments without the program name; ``None`` for sys.argv.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
    )
    parser.add_argument(
        "--repeat",
        type=_cli.positive_int,
        metavar="N",
        default=5,
        help="timed runs per case, median reported (default: %(default)s)",
    )
    parser.add_argument(
        "--producers",
        type=_cli.positive_int,
        metavar="N",
        default=4,
        help="processes, or in-process sources (default: %(default)s)",
    )
    parser.add_argument(
        "--workers",
        type=_cli.positive_int,
        metavar="N",
        default=4,
        help="handler threads (default: %(default)s)",
    )
    parser.add_argument(
        "--items",
        type=_cli.positive_int,
        metavar="N",
        default=10_000,
        help="transport items per producer (default: %(default)s)",
    )
    parser.add_argument(
        "--cpu-items",
        type=_cli.positive_int,
        metavar="N",
        default=500,
        help="cpu items per producer (default: %(default)s)",
    )
    parser.add_argument(
        "--rounds",
        type=_cli.positive_int,
        metavar="N",
        default=4_000,
        help="arithmetic steps per cpu item (default: %(default)s)",
    )
    parser.add_argument(
        "--batch-size",
        type=_cli.positive_int,
        metavar="N",
        default=64,
        help="items per message, batched cases (default: %(default)s)",
    )
    parser.add_argument(
        "--start-method",
        choices=multiprocessing.get_all_start_methods(),
        help="unset means the platform default",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="tiny sizes and one repetition: a smoke test, not a measurement",
    )
    args = parser.parse_args(argv)
    if args.quick:
        args.repeat, args.items, args.cpu_items, args.rounds = 1, 200, 20, 100
    return args


def build_cases(args: argparse.Namespace) -> list[Case]:
    """Returns the workload x transport matrix to time."""

    def sources(make: Source[int]) -> dict[str, Source[int]]:
        return {f"p{i}": make for i in range(args.producers)}

    idle = sources(functools.partial(numbers, 0))
    transport = sources(functools.partial(numbers, args.items))
    cpu = sources(functools.partial(cpu_numbers, args.cpu_items, args.rounds))
    pipes = functools.partial(
        run_pipeline,
        handler=noop,
        workers=args.workers,
        start_method=args.start_method,
    )
    in_process = functools.partial(
        run_in_process, handler=noop, workers=args.workers
    )
    batch = args.batch_size
    return [
        Case("startup", "in-process", functools.partial(in_process, idle), 0),
        Case("startup", "pipes", functools.partial(pipes, idle), 0),
        Case(
            "transport",
            "in-process",
            functools.partial(in_process, transport),
            args.producers * args.items,
        ),
        Case(
            "transport",
            "pipes, batch 1",
            functools.partial(pipes, transport, batch_size=1),
            args.producers * args.items,
        ),
        Case(
            "transport",
            f"pipes, batch {batch}",
            functools.partial(pipes, transport, batch_size=batch),
            args.producers * args.items,
        ),
        Case(
            "cpu",
            "in-process",
            functools.partial(in_process, cpu),
            args.producers * args.cpu_items,
        ),
        Case(
            "cpu",
            f"pipes, batch {batch}",
            functools.partial(pipes, cpu, batch_size=batch),
            args.producers * args.cpu_items,
        ),
    ]


def measure(case: Case, repeat: int) -> list[float]:
    """Times ``repeat`` runs of ``case``, verifying each one.

    Raises:
        BenchmarkError: If a run did not process exactly the expected items.
    """
    times: list[float] = []
    for _ in range(repeat):
        start = time.perf_counter()
        report = case.run()
        elapsed = time.perf_counter() - start
        if not report.ok or report.processed != case.expected:
            raise BenchmarkError(
                f"{case.workload}/{case.mode}: processed {report.processed} "
                f"of {case.expected}, ok={report.ok}"
            )
        times.append(elapsed)
    return times


def environment(args: argparse.Namespace) -> list[str]:
    """Describes everything that affects the numbers."""
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    method = args.start_method or multiprocessing.get_start_method()
    return [
        f"- Python {platform.python_version()} "
        f"({platform.python_implementation()}, "
        f"GIL {'enabled' if gil else 'disabled'}) on {platform.platform()}, "
        f"{os.cpu_count()} CPUs",
        f"- start method {method}; {args.producers} producers, "
        f"{args.workers} worker threads, noop handler; "
        f"median of {args.repeat} runs",
        f"- transport: {args.items} items per producer; cpu: "
        f"{args.cpu_items} items per producer x {args.rounds} rounds",
    ]


def _run(args: argparse.Namespace) -> int:
    lines = [
        *environment(args),
        "",
        "| workload | mode | median s | min s | max s | items/s |",
        "|---|---|---:|---:|---:|---:|",
    ]
    print("\n".join(lines), flush=True)
    for case in build_cases(args):
        try:
            times = measure(case, args.repeat)
        except BenchmarkError as exc:
            LOG.error("%s", exc)
            return _cli.EXIT_FAILURE
        median = statistics.median(times)
        rate = f"{case.expected / median:,.0f}" if case.expected else "-"
        print(
            f"| {case.workload} | {case.mode} | {median:.3f} | "
            f"{min(times):.3f} | {max(times):.3f} | {rate} |",
            flush=True,
        )
    return _cli.EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the benchmark and prints a Markdown table to stdout.

    Args:
        argv: Arguments without the program name; ``None`` for sys.argv.

    Returns:
        0 on success; 1 if a run was incorrect; 2 for bad arguments; 130 or
        143 after a clean shutdown on SIGINT or SIGTERM.
    """
    args = parse_args(argv)
    _cli.configure_logging(0)
    return _cli.run_main(lambda: _run(args))


if __name__ == "__main__":
    sys.exit(main())
