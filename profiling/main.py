"""Profile a workload with planted problems and compare what each tool says.

Usage: ``python3 main.py [command] [options]``; ``-h`` lists both.

Exit status: 0 on success, 1 on a runtime failure (such as an unwritable
output file), 2 on a usage error, 130 after SIGINT and 143 after SIGTERM.
"""

import argparse
import functools
import logging
import math
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

import blindspots
import clocks
import memory
import overhead
import profiler
import sampler
import workload

__all__ = ["COMMANDS", "main"]

PROG = "profiling"
LOG = logging.getLogger(PROG)

EXIT_OK = 0
EXIT_FAILURE = 1
# Usage errors exit 2 from argparse itself.

MAX_TOP = 50
MAX_SAMPLES = 100_000
MAX_LEAK = 1_000_000
CLOCK_DEMO_S = 0.05
CHILD_WORK = 200_000
THREAD_WORK = 1_000_000


class Terminated(BaseException):
    """Raised by the SIGTERM handler so cleanup runs like it does on Ctrl+C."""


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"must be an integer, got {text!r}"
            ) from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"must be in {low}..{high}, got {value}"
            )
        return value

    return parse


def _interval_ms(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be a number, got {text!r}"
        ) from None
    low, high = sampler.MIN_INTERVAL_S * 1000, sampler.MAX_INTERVAL_S * 1000
    if not math.isfinite(value) or not low <= value <= high:
        raise argparse.ArgumentTypeError(
            f"must be in {low:g}..{high:g}, got {text}"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Profile a workload with planted, known problems.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=("all", *COMMANDS),
        help="what to show (default: %(default)s, everything but bench)",
    )
    parser.add_argument(
        "--size",
        type=_bounded_int(1, workload.MAX_REPORT_SIZE),
        default=4000,
        metavar="N",
        help="IDs in the report workload (default: %(default)s)",
    )
    parser.add_argument(
        "--calls",
        type=_bounded_int(1, workload.MAX_CALLS),
        default=300_000,
        metavar="N",
        help="elements in the bench workloads (default: %(default)s)",
    )
    parser.add_argument(
        "--repetitions",
        type=_bounded_int(1, overhead.MAX_REPETITIONS),
        default=5,
        metavar="N",
        help="bench runs per workload and mode (default: %(default)s)",
    )
    parser.add_argument(
        "--top",
        type=_bounded_int(1, MAX_TOP),
        default=5,
        metavar="N",
        help="rows per table (default: %(default)s)",
    )
    parser.add_argument(
        "--interval-ms",
        type=_interval_ms,
        default=1.0,
        metavar="MS",
        help="sampling interval in ms of CPU time (default: %(default)s)",
    )
    parser.add_argument(
        "--samples",
        type=_bounded_int(1, MAX_SAMPLES),
        default=300,
        metavar="N",
        help="minimum samples to collect (default: %(default)s)",
    )
    parser.add_argument(
        "--leak",
        type=_bounded_int(1, MAX_LEAK),
        default=2000,
        metavar="N",
        help="payloads the leak demo keeps alive (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/workload.prof"),
        metavar="PATH",
        help="where cprofile saves its .prof file (default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log progress to stderr (-v INFO, -vv DEBUG)",
    )
    return parser


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def show_cprofile(args: argparse.Namespace) -> None:
    """Deterministic profile of the report: tottime, cumtime, callers."""
    _, stats = profiler.profile_call(workload.run_report, args.size)
    _section(f"cProfile: run_report({args.size}) by tottime (own time)")
    print(profiler.format_table(profiler.top_functions(stats, limit=args.top)))
    _section("cProfile: by cumtime (including callees)")
    rows = profiler.top_functions(stats, sort="cumtime", limit=args.top)
    print(profiler.format_table(rows))
    path = profiler.save(stats, args.output)
    print(f"\nsaved {path}; explore it with: python3 -m pstats {path}")


def show_sampler(args: argparse.Namespace) -> None:
    """Statistical profile of the same report."""
    result = sampler.sample_until(
        functools.partial(workload.run_report, args.size),
        interval=args.interval_ms / 1000,
        min_samples=args.samples,
    )
    _section(
        f"sampler: {result.samples} samples every {args.interval_ms:g} ms "
        "of CPU time"
    )
    print(sampler.format_table(result.top(args.top)))


def show_memory(args: argparse.Namespace) -> None:
    """Where memory grows while the leak runs."""
    workload.forget()
    try:
        growth = memory.find_growth(
            functools.partial(workload.leak, args.leak), limit=args.top
        )
    finally:
        workload.forget()
    _section(f"tracemalloc: growth while leaking {args.leak} payloads")
    print(memory.format_table(growth))


def show_timeit(args: argparse.Namespace) -> None:
    """The hotspot versus its fix, measured with timeit."""
    ids = workload.make_ids(args.size)
    slow = clocks.time_per_loop(
        functools.partial(workload.find_duplicates, ids), number=3
    )
    fast = clocks.time_per_loop(
        functools.partial(workload.find_duplicates_fast, ids), number=3
    )
    _section(f"timeit: find_duplicates on {args.size} IDs, best of 5")
    print(f"list membership (planted) {slow.best * 1e3:9.3f} ms per call")
    print(f"set membership (fix)      {fast.best * 1e3:9.3f} ms per call")
    print(f"speed-up                  {slow.best / fast.best:9.0f}x")


def show_clocks(_: argparse.Namespace) -> None:
    """How each clock moves for sleep, local CPU and another thread's CPU."""
    scenarios = clocks.clock_scenarios(CLOCK_DEMO_S)
    _section(f"clocks: seconds each clock advanced, {CLOCK_DEMO_S} s of work")
    print(f"{'scenario':<22} {'perf_counter':>12} {'process':>8} {'thread':>8}")
    for name, delta in scenarios.items():
        print(
            f"{name:<22} {delta.wall:12.3f} {delta.process_cpu:8.3f} "
            f"{delta.thread_cpu:8.3f}"
        )


def show_blindspots(args: argparse.Namespace) -> None:
    """What a parent's cProfile misses: children, and threads by version."""
    _section("blind spots: child processes")
    for method in blindspots.START_METHODS:
        seen = blindspots.parent_sees_child(method, CHILD_WORK)
        print(f"{method:<10} parent's profile contains child_hotspot: {seen}")
    merged = blindspots.profile_children(
        "spawn", 2, CHILD_WORK, args.output.parent / "children"
    )
    calls = merged.get_stats_profile().func_profiles["child_hotspot"].ncalls
    print(f"profiled in each child and merged: child_hotspot calls = {calls}")
    finding = blindspots.thread_finding(THREAD_WORK)
    _section(f"blind spots: threads (Python {finding.python})")
    print(f"other thread recorded: {finding.worker_seen}")
    print(
        f"worker: cProfile says {finding.worker_reported:.3f} s, "
        f"it used {finding.worker_cpu:.3f} s of CPU"
    )
    print(
        f"main:   cProfile says {finding.main_reported:.3f} s, "
        f"it used {finding.main_cpu:.3f} s of CPU"
    )
    error = finding.second_profiler_error or "none, allowed"
    print(f"second profiler in another thread: {error}")


def show_bench(args: argparse.Namespace) -> None:
    """CProfile's and the sampler's overhead on call-heavy vs coarse code."""
    _section("bench: profiler overhead, median wall-clock time")
    print(overhead.environment())
    print(
        f"method: {args.calls} elements, {args.repetitions} repetitions, "
        "modes interleaved, median reported, results verified"
    )
    rows = overhead.measure_overhead(
        args.calls,
        repetitions=args.repetitions,
        interval=args.interval_ms / 1000,
    )
    print(overhead.format_table(rows))


COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "cprofile": show_cprofile,
    "sample": show_sampler,
    "memory": show_memory,
    "timeit": show_timeit,
    "clocks": show_clocks,
    "blindspots": show_blindspots,
    "bench": show_bench,
}
_ALL = ("cprofile", "sample", "memory", "timeit", "clocks", "blindspots")


class _SignalState:
    """Whether a shutdown signal arrived. Only the main thread touches it."""

    received = False


def _on_signal(signum: int, _frame: FrameType | None) -> None:
    if _SignalState.received:
        os._exit(128 + signum)  # Second signal: don't wait for cleanup.
    _SignalState.received = True
    name = signal.Signals(signum).name
    # os.write is async-signal-safe; print and logging are not.
    os.write(2, f"\n{PROG}: {name} received, stopping\n".encode())
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise Terminated


def _install_signal_handlers() -> None:
    # Installed explicitly, so this works even when SIGINT was inherited as
    # ignored (a process started with & from a non-interactive shell).
    _SignalState.received = False
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the command named on the command line.

    Args:
        argv: Arguments without the program name; ``None`` means
            ``sys.argv[1:]``.

    Returns:
        The exit status.
    """
    args = _parser().parse_args(argv)
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)]
    logging.basicConfig(
        level=level, format="%(levelname)s %(message)s", stream=sys.stderr
    )
    _install_signal_handlers()
    names = _ALL if args.command == "all" else (args.command,)
    print(f"running: {' '.join(names)}", flush=True)
    try:
        for name in names:
            LOG.info("running %s", name)
            COMMANDS[name](args)
            sys.stdout.flush()
    except KeyboardInterrupt:
        LOG.warning("interrupted by SIGINT; stopped cleanly")
        return 128 + signal.SIGINT
    except Terminated:
        LOG.warning("terminated by SIGTERM; stopped cleanly")
        return 128 + signal.SIGTERM
    except (OSError, RuntimeError) as exc:  # TimeoutError is an OSError.
        LOG.error("%s", exc)
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
