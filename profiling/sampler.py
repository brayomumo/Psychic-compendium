"""A minimal statistical (sampling) CPU profiler built on SIGPROF.

Instead of recording every call, the kernel interrupts the process at a fixed
interval of *CPU time* (``ITIMER_PROF``), and the handler records which stack
was running. A function's share of the samples estimates its share of CPU
time, and the cost is one handler call per interval, however many calls the
program makes.

Limits of this implementation, which real sampling profilers (py-spy) avoid
by reading the process from outside:

* Python runs signal handlers in the main thread, between bytecodes. Only the
  main thread is sampled, and a sample that arrives during a long C call is
  taken when the call returns, so it lands on the line after it.
* ``ITIMER_PROF`` counts CPU time, so time spent sleeping or waiting for I/O
  produces no samples. Use ``ITIMER_REAL`` for wall-clock sampling.
* It is Unix-only, and owns the process's single ``ITIMER_PROF`` timer.
"""

import signal
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType
from typing import Self

__all__ = [
    "MAX_INTERVAL_S",
    "MIN_INTERVAL_S",
    "FunctionKey",
    "SampleStat",
    "Sampler",
    "format_table",
    "sample_until",
]

MIN_INTERVAL_S = 0.0001
MAX_INTERVAL_S = 1.0


@dataclass(frozen=True, order=True)
class FunctionKey:
    """Identifies a Python function by where its code object was defined.

    Attributes:
        qualname: Qualified name, such as ``Sampler.__exit__``.
        filename: Source file.
        firstlineno: Line of the ``def``.
    """

    qualname: str
    filename: str
    firstlineno: int

    def __str__(self) -> str:
        """Returns ``qualname (file:line)``."""
        where = f"{Path(self.filename).name}:{self.firstlineno}"
        return f"{self.qualname} ({where})"


@dataclass(frozen=True)
class SampleStat:
    """How often a function was seen in the samples.

    Attributes:
        function: The function.
        self_samples: Samples where it was the running (innermost) frame.
        total_samples: Samples where it was anywhere on the stack.
        self_share: ``self_samples`` as a fraction of all samples.
        total_share: ``total_samples`` as a fraction of all samples.
    """

    function: FunctionKey
    self_samples: int
    total_samples: int
    self_share: float
    total_share: float


class Sampler:
    """Samples the main thread's stack every ``interval`` seconds of CPU time.

    Use it as a context manager in the main thread::

        with Sampler(interval=0.001) as sampler:
            run_report(4000)
        print(format_table(sampler.top()))
    """

    def __init__(self, interval: float = 0.001) -> None:
        """Creates an idle sampler.

        Args:
            interval: Seconds of CPU time between samples, in
                ``MIN_INTERVAL_S..MAX_INTERVAL_S``.

        Raises:
            ValueError: ``interval`` is out of range (or NaN).
        """
        if not MIN_INTERVAL_S <= interval <= MAX_INTERVAL_S:
            raise ValueError(
                f"interval must be in {MIN_INTERVAL_S}..{MAX_INTERVAL_S} "
                f"seconds, got {interval}"
            )
        self._interval = interval
        self._stacks: Counter[tuple[FunctionKey, ...]] = Counter()
        self._previous: Callable[[int, FrameType | None], object] | int | None
        self._previous = None
        self._running = False

    @property
    def samples(self) -> int:
        """Number of samples taken so far."""
        return self._stacks.total()

    def __enter__(self) -> Self:
        """Starts sampling.

        Raises:
            RuntimeError: Not in the main thread, already running, or another
                ``ITIMER_PROF`` timer is active.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("signal handlers only run in the main thread")
        if self._running:
            raise RuntimeError("this sampler is already running")
        if signal.getitimer(signal.ITIMER_PROF) != (0.0, 0.0):
            raise RuntimeError("another ITIMER_PROF timer is already active")
        self._previous = signal.signal(signal.SIGPROF, self._on_sample)
        self._running = True
        signal.setitimer(signal.ITIMER_PROF, self._interval, self._interval)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stops sampling and restores the previous SIGPROF handler."""
        signal.setitimer(signal.ITIMER_PROF, 0.0, 0.0)
        previous = self._previous
        signal.signal(
            signal.SIGPROF, signal.SIG_DFL if previous is None else previous
        )
        self._running = False

    def _on_sample(self, _signum: int, frame: FrameType | None) -> None:
        stack: list[FunctionKey] = []
        while frame is not None:
            code = frame.f_code
            key = FunctionKey(
                code.co_qualname, code.co_filename, code.co_firstlineno
            )
            stack.append(key)
            frame = frame.f_back
        stack.reverse()  # Outermost first, like a traceback.
        self._stacks[tuple(stack)] += 1

    def top(self, limit: int = 5) -> list[SampleStat]:
        """Ranks functions by self samples.

        Args:
            limit: How many rows to return, at least 1.

        Returns:
            The top rows, most self samples first.

        Raises:
            ValueError: ``limit`` is less than 1.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        own: Counter[FunctionKey] = Counter()
        total: Counter[FunctionKey] = Counter()
        for stack, count in self._stacks.items():
            if stack:
                own[stack[-1]] += count
            for function in set(stack):  # Recursion counts once per sample.
                total[function] += count
        samples = max(1, self.samples)
        ranked = sorted(total, key=lambda f: (-own[f], -total[f], f))
        return [
            SampleStat(
                function=f,
                self_samples=own[f],
                total_samples=total[f],
                self_share=own[f] / samples,
                total_share=total[f] / samples,
            )
            for f in ranked[:limit]
        ]


def sample_until(
    workload: Callable[[], object],
    *,
    interval: float = 0.001,
    min_samples: int = 200,
    timeout_s: float = 30.0,
) -> Sampler:
    """Calls ``workload`` repeatedly under a :class:`Sampler`.

    Sampling is statistical, so a stable answer needs enough samples:
    ``workload`` runs again and again until ``min_samples`` have been taken.

    Args:
        workload: What to sample, taking no arguments (use
            ``functools.partial`` to bind some).
        interval: Seconds of CPU time between samples.
        min_samples: Samples to collect, at least 1.
        timeout_s: Wall-clock limit, so a workload that uses no CPU (and
            therefore produces no samples) can't loop forever.

    Returns:
        The stopped sampler, holding the samples.

    Raises:
        ValueError: ``min_samples`` or ``interval`` is invalid.
        TimeoutError: Fewer than ``min_samples`` within ``timeout_s``.
    """
    if min_samples < 1:
        raise ValueError(f"min_samples must be >= 1, got {min_samples}")
    deadline = time.monotonic() + timeout_s
    with Sampler(interval) as sampler:
        while sampler.samples < min_samples:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"only {sampler.samples} of {min_samples} samples "
                    f"after {timeout_s} s"
                )
            workload()
    return sampler


def format_table(rows: list[SampleStat]) -> str:
    """Formats rows as a fixed-width table.

    Args:
        rows: Rows from :meth:`Sampler.top`.

    Returns:
        The table, one row per line, with a header.
    """
    lines = [f"{'self':>7} {'total':>7}  function"]
    for row in rows:
        lines.append(
            f"{row.self_share:7.1%} {row.total_share:7.1%}  {row.function}"
        )
    return "\n".join(lines)
