"""Measuring small things: which clock, and how to use timeit.

Python's clocks answer different questions:

* ``time.perf_counter()``: elapsed wall-clock time, monotonic, the highest
  resolution available. "How long did the caller wait?"
* ``time.process_time()``: CPU time used by the whole process, every thread,
  user plus system. It doesn't advance while sleeping or blocked on I/O.
  "How much CPU did this burn?"
* ``time.thread_time()``: CPU time used by the calling thread only.

For micro-benchmarks, ``timeit`` runs a statement many times per measurement,
repeats the measurement, and disables the garbage collector while timing.
Take the *minimum* of the repeats: interference (other processes, cache
misses, frequency changes) only ever adds time, so the fastest run is the
closest to what the code itself costs. End-to-end benchmarks are a different
question (what users see, noise included) and report a median instead.
"""

import threading
import time
import timeit
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "MAX_DURATION_S",
    "ClockDeltas",
    "TimeitResult",
    "burn_cpu",
    "clock_scenarios",
    "measure_clocks",
    "time_per_loop",
]

MAX_DURATION_S = 10.0


@dataclass(frozen=True)
class ClockDeltas:
    """How far each clock advanced during one piece of work, in seconds.

    Attributes:
        wall: ``perf_counter`` delta.
        process_cpu: ``process_time`` delta.
        thread_cpu: ``thread_time`` delta of the measuring thread.
    """

    wall: float
    process_cpu: float
    thread_cpu: float


@dataclass(frozen=True)
class TimeitResult:
    """Per-loop timings from ``timeit``, in seconds.

    Attributes:
        best: Fastest repeat divided by ``number``: the figure to report.
        worst: Slowest repeat divided by ``number``: shows the noise.
        number: Executions per repeat.
        repeat: Number of repeats.
    """

    best: float
    worst: float
    number: int
    repeat: int


def measure_clocks(work: Callable[[], object]) -> ClockDeltas:
    """Runs ``work`` and reports how far each clock moved.

    Args:
        work: What to measure, taking no arguments.

    Returns:
        The three deltas.
    """
    wall, cpu, thread = (
        time.perf_counter(),
        time.process_time(),
        time.thread_time(),
    )
    work()
    return ClockDeltas(
        wall=time.perf_counter() - wall,
        process_cpu=time.process_time() - cpu,
        thread_cpu=time.thread_time() - thread,
    )


def burn_cpu(seconds: float) -> int:
    """Spins until the calling thread has used ``seconds`` of CPU time.

    Measuring by ``thread_time`` rather than wall-clock time makes the work
    the same however busy the machine is.

    Args:
        seconds: CPU time to use, in ``0..MAX_DURATION_S``.

    Returns:
        How many loop iterations it took.

    Raises:
        ValueError: ``seconds`` is out of range (or NaN).
    """
    _check_duration(seconds)
    deadline = time.thread_time() + seconds
    spins = 0
    while time.thread_time() < deadline:
        spins += 1
    return spins


def _in_another_thread(seconds: float) -> None:
    thread = threading.Thread(target=burn_cpu, args=(seconds,), name="burner")
    thread.start()
    thread.join()


def clock_scenarios(duration: float) -> dict[str, ClockDeltas]:
    """Shows each clock on three kinds of work of the same length.

    Args:
        duration: Seconds of sleep or CPU per scenario, in
            ``0..MAX_DURATION_S``.

    Returns:
        Deltas for ``"sleep"``, ``"cpu in this thread"`` and
        ``"cpu in another thread"``.

    Raises:
        ValueError: ``duration`` is out of range (or NaN).
    """
    _check_duration(duration)
    return {
        "sleep": measure_clocks(lambda: time.sleep(duration)),
        "cpu in this thread": measure_clocks(lambda: burn_cpu(duration)),
        "cpu in another thread": measure_clocks(
            lambda: _in_another_thread(duration)
        ),
    }


def time_per_loop(
    stmt: Callable[[], object], *, number: int | None = None, repeat: int = 5
) -> TimeitResult:
    """Times ``stmt`` the way ``python -m timeit`` does.

    Args:
        stmt: What to time, taking no arguments.
        number: Executions per repeat, at least 1. ``None`` lets
            ``Timer.autorange`` pick a count that takes at least 0.2 s.
        repeat: Number of repeats, at least 1.

    Returns:
        Best and worst per-loop times.

    Raises:
        ValueError: ``number`` or ``repeat`` is less than 1.
    """
    if repeat < 1:
        raise ValueError(f"repeat must be >= 1, got {repeat}")
    if number is not None and number < 1:
        raise ValueError(f"number must be >= 1, got {number}")
    timer = timeit.Timer(stmt)
    if number is None:
        number, _ = timer.autorange()
    times = timer.repeat(repeat=repeat, number=number)
    return TimeitResult(
        best=min(times) / number,
        worst=max(times) / number,
        number=number,
        repeat=repeat,
    )


def _check_duration(seconds: float) -> None:
    if not 0.0 <= seconds <= MAX_DURATION_S:
        raise ValueError(
            f"duration must be in 0..{MAX_DURATION_S} seconds, got {seconds}"
        )
