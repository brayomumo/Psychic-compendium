"""The observer effect: what profiling costs, and which code pays for it.

cProfile does work on every call and return, so its overhead is proportional
to the number of calls, not to the time spent. A function that makes a
million tiny calls is slowed down several times over, while a loop doing the
same arithmetic inline barely changes. Its *relative* times are therefore
distorted: call-heavy code looks more expensive than it is. A sampling
profiler costs one handler call per interval, whatever the code does.
"""

import cProfile
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import workload
from sampler import Sampler

__all__ = [
    "MAX_REPETITIONS",
    "MODES",
    "WORKLOADS",
    "OverheadRow",
    "Timing",
    "environment",
    "format_table",
    "measure_overhead",
]

MAX_REPETITIONS = 1000

WORKLOADS: dict[str, Callable[[int], int]] = {
    "call-heavy": workload.sum_of_squares_calls,
    "coarse": workload.sum_of_squares_inline,
}
"""Same arithmetic, one Python call per element versus none."""

MODES = ("plain", "cprofile", "sampler")


@dataclass(frozen=True)
class Timing:
    """Summary of repeated wall-clock measurements, in seconds.

    Attributes:
        median: The figure reported.
        minimum: Fastest run.
        maximum: Slowest run.
    """

    median: float
    minimum: float
    maximum: float

    @classmethod
    def of(cls, samples: Sequence[float]) -> "Timing":
        """Summarises ``samples``, which must not be empty."""
        return cls(statistics.median(samples), min(samples), max(samples))


@dataclass(frozen=True)
class OverheadRow:
    """One workload timed without a profiler, under cProfile and sampled.

    Attributes:
        workload: Key in :data:`WORKLOADS`.
        plain: No profiler.
        cprofile: Under ``cProfile``.
        sampler: Under :class:`sampler.Sampler`.
    """

    workload: str
    plain: Timing
    cprofile: Timing
    sampler: Timing

    @property
    def cprofile_factor(self) -> float:
        """Median slowdown under cProfile (1.0 means no overhead)."""
        return self.cprofile.median / self.plain.median

    @property
    def sampler_factor(self) -> float:
        """Median slowdown under the sampler."""
        return self.sampler.median / self.plain.median


def _timed(
    mode: str, func: Callable[[int], int], n: int, interval: float
) -> tuple[float, int]:
    if mode == "cprofile":
        profiler = cProfile.Profile()
        start = time.perf_counter()
        profiler.enable()
        try:
            result = func(n)
        finally:
            # Always unregister: on 3.12+ a profiler left enabled blocks
            # every later one in this interpreter.
            profiler.disable()
        return time.perf_counter() - start, result
    if mode == "sampler":
        with Sampler(interval):
            start = time.perf_counter()
            result = func(n)
            return time.perf_counter() - start, result
    start = time.perf_counter()
    result = func(n)
    return time.perf_counter() - start, result


def measure_overhead(
    n: int, *, repetitions: int = 5, interval: float = 0.001
) -> list[OverheadRow]:
    """Times every workload in every mode.

    Each repetition runs the modes back to back, so slow drift (thermal
    throttling, other load) affects every mode alike. Every run's result is
    checked against an unprofiled warm-up run before its time counts.

    Args:
        n: Elements per workload, in ``0..workload.MAX_CALLS``.
        repetitions: Runs per workload and mode, in
            ``1..MAX_REPETITIONS``.
        interval: Sampling interval in seconds of CPU time.

    Returns:
        One row per workload, in :data:`WORKLOADS` order.

    Raises:
        ValueError: An argument is out of range.
        RuntimeError: A profiled run returned a different result.
    """
    if not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(
            f"repetitions must be in 1..{MAX_REPETITIONS}, got {repetitions}"
        )
    Sampler(interval)  # Validates the interval before any work starts.
    rows: list[OverheadRow] = []
    for name, func in WORKLOADS.items():
        expected = func(n)  # Also warms up caches and the specializer.
        samples: dict[str, list[float]] = {mode: [] for mode in MODES}
        for _ in range(repetitions):
            for mode in MODES:
                elapsed, result = _timed(mode, func, n, interval)
                if result != expected:
                    raise RuntimeError(
                        f"{name} under {mode} returned {result}, "
                        f"expected {expected}"
                    )
                samples[mode].append(elapsed)
        rows.append(
            OverheadRow(
                workload=name,
                plain=Timing.of(samples["plain"]),
                cprofile=Timing.of(samples["cprofile"]),
                sampler=Timing.of(samples["sampler"]),
            )
        )
    return rows


def environment() -> str:
    """Describes the machine, for the header of benchmark output."""
    load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
    return (
        f"{platform.system()} {platform.release()} {platform.machine()}, "
        f"{os.cpu_count()} CPUs, load {load}, "
        f"Python {platform.python_version()} "
        f"({sys.implementation.name})"
    )


def _cell(timing: Timing) -> str:
    low, high = timing.minimum * 1e3, timing.maximum * 1e3
    return f"{timing.median * 1e3:.1f} ms ({low:.1f}-{high:.1f})"


def format_table(rows: Sequence[OverheadRow]) -> str:
    """Formats rows as a Markdown table: median (min-max), then slowdown.

    Args:
        rows: Rows from :func:`measure_overhead`.

    Returns:
        The table, with a header.
    """
    lines = [
        "| workload | plain | cProfile | slowdown | sampler | slowdown |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.workload} | {_cell(row.plain)} | {_cell(row.cprofile)} "
            f"| x{row.cprofile_factor:.2f} | {_cell(row.sampler)} "
            f"| x{row.sampler_factor:.2f} |"
        )
    return "\n".join(lines)
