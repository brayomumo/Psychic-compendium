"""Bulkheads: cap how much of a process one dependency can occupy.

Three ways to call a dependency, with one interface so they can be compared
under the same load:

* ``Unisolated`` calls it directly on the caller's thread. Nothing stops one
  slow dependency from tying up every caller.
* ``SemaphoreBulkhead`` lets at most ``limit`` callers be inside the
  dependency at once and rejects the rest, immediately or after a bounded
  wait. It is cheap, but a caller that gets in stays stuck for as long as the
  call takes.
* ``ThreadPoolBulkhead`` runs calls on the dependency's own small pool and
  waits at most ``timeout`` for each result. The caller always gets its thread
  back on time; a hung call keeps its pool worker, and its capacity, until it
  returns, because Python cannot kill a thread.
"""

import math
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, TypeVar

__all__ = [
    "MAX_LIMIT",
    "MAX_QUEUE",
    "MAX_TIMEOUT_S",
    "BulkheadFullError",
    "CallTimeoutError",
    "Isolation",
    "SemaphoreBulkhead",
    "Stats",
    "ThreadPoolBulkhead",
    "Unisolated",
]

T = TypeVar("T")

# Guardrails on configuration. Each concurrent call can cost a thread, so a
# mistyped limit must not be allowed to spawn thousands of them.
MAX_LIMIT = 1024
MAX_QUEUE = 10_000
MAX_TIMEOUT_S = 3600.0


class BulkheadFullError(RuntimeError):
    """The dependency's bulkhead was at capacity, so the call was not made."""


class CallTimeoutError(TimeoutError):
    """The caller stopped waiting. The call itself may still be running."""


@dataclass(frozen=True)
class Stats:
    """A consistent snapshot of one isolation's counters.

    Attributes:
        accepted: Calls admitted (and, for a pool, handed to a worker queue).
        rejected: Calls refused because the bulkhead was full.
        timed_out: Calls the caller abandoned after its timeout.
        in_flight: Calls currently executing inside the dependency.
        peak_in_flight: Highest ``in_flight`` ever observed.
    """

    accepted: int
    rejected: int
    timed_out: int
    in_flight: int
    peak_in_flight: int


class Isolation(Protocol):
    """How a service calls one dependency."""

    @property
    def name(self) -> str:
        """The dependency this isolation guards."""
        ...

    def call(self, fn: Callable[[], T]) -> T:
        """Calls ``fn`` under this isolation's rules and returns its result."""
        ...

    def stats(self) -> Stats:
        """Returns a snapshot of the counters."""
        ...

    def wait_until(
        self, predicate: Callable[[Stats], bool], timeout: float | None
    ) -> bool:
        """Blocks until ``predicate(stats)`` holds or ``timeout`` passes."""
        ...

    def close(self) -> None:
        """Releases any threads this isolation owns."""
        ...


class _Gauge:
    """Counters that observers can wait on, instead of polling them."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._accepted = 0
        self._rejected = 0
        self._timed_out = 0
        self._in_flight = 0
        self._peak = 0

    def _snapshot(self) -> Stats:
        return Stats(
            self._accepted,
            self._rejected,
            self._timed_out,
            self._in_flight,
            self._peak,
        )

    def snapshot(self) -> Stats:
        with self._cond:
            return self._snapshot()

    def wait_until(
        self, predicate: Callable[[Stats], bool], timeout: float | None
    ) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: predicate(self._snapshot()), timeout
            )

    def accepted(self) -> None:
        with self._cond:
            self._accepted += 1
            self._cond.notify_all()

    def rejected(self) -> None:
        with self._cond:
            self._rejected += 1
            self._cond.notify_all()

    def timed_out(self) -> None:
        with self._cond:
            self._timed_out += 1
            self._cond.notify_all()

    def enter(self) -> None:
        with self._cond:
            self._in_flight += 1
            self._peak = max(self._peak, self._in_flight)
            self._cond.notify_all()

    def exit(self) -> None:
        with self._cond:
            self._in_flight -= 1
            self._cond.notify_all()

    def run(self, fn: Callable[[], T]) -> T:
        self.enter()
        try:
            return fn()
        finally:
            self.exit()


def _check_int(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be in {low}..{high}, got {value}")


def _check_seconds(name: str, value: float, *, allow_zero: bool) -> None:
    if not math.isfinite(value) or value < 0 or value > MAX_TIMEOUT_S:
        raise ValueError(
            f"{name} must be a finite number of seconds in "
            f"0..{MAX_TIMEOUT_S:g}, got {value}"
        )
    if value == 0 and not allow_zero:
        raise ValueError(f"{name} must be greater than 0")


class Unisolated:
    """Calls the dependency directly: the baseline with no protection."""

    def __init__(self, name: str) -> None:
        """Creates the baseline isolation.

        Args:
            name: The dependency's name, for reports.
        """
        self._name = name
        self._gauge = _Gauge()

    @property
    def name(self) -> str:
        """The dependency this isolation guards."""
        return self._name

    def call(self, fn: Callable[[], T]) -> T:
        """Calls ``fn`` on the caller's thread, for as long as it takes."""
        self._gauge.accepted()
        return self._gauge.run(fn)

    def stats(self) -> Stats:
        """Returns a snapshot of the counters."""
        return self._gauge.snapshot()

    def wait_until(
        self, predicate: Callable[[Stats], bool], timeout: float | None
    ) -> bool:
        """Blocks until ``predicate(stats)`` holds or ``timeout`` passes."""
        return self._gauge.wait_until(predicate, timeout)

    def close(self) -> None:
        """Does nothing: this isolation owns no threads."""


class SemaphoreBulkhead:
    """Admits at most ``limit`` concurrent calls; rejects the rest.

    The call runs on the caller's thread. The bulkhead bounds how many callers
    one dependency can hold, which protects the caller's shared thread pool,
    but it cannot rescue a caller that is already inside a hung call.
    """

    def __init__(self, name: str, limit: int, max_wait: float = 0.0) -> None:
        """Creates the bulkhead.

        Args:
            name: The dependency's name, for reports and errors.
            limit: Most callers allowed inside the dependency at once.
            max_wait: Seconds a caller may wait for a free slot before being
                rejected. ``0`` rejects immediately (fail fast).

        Raises:
            ValueError: If ``limit`` or ``max_wait`` is out of range.
        """
        _check_int("limit", limit, 1, MAX_LIMIT)
        _check_seconds("max_wait", max_wait, allow_zero=True)
        self._name = name
        self._limit = limit
        self._max_wait = max_wait
        self._slots = threading.BoundedSemaphore(limit)
        self._gauge = _Gauge()

    @property
    def name(self) -> str:
        """The dependency this bulkhead guards."""
        return self._name

    def call(self, fn: Callable[[], T]) -> T:
        """Calls ``fn`` if a slot is free (within ``max_wait``).

        Raises:
            BulkheadFullError: If no slot became free in time.
        """
        if self._max_wait > 0:
            admitted = self._slots.acquire(timeout=self._max_wait)
        else:
            admitted = self._slots.acquire(blocking=False)
        if not admitted:
            self._gauge.rejected()
            raise BulkheadFullError(
                f"{self._name}: {self._limit} calls already in flight"
            )
        try:
            self._gauge.accepted()
            return self._gauge.run(fn)
        finally:
            self._slots.release()

    def stats(self) -> Stats:
        """Returns a snapshot of the counters."""
        return self._gauge.snapshot()

    def wait_until(
        self, predicate: Callable[[Stats], bool], timeout: float | None
    ) -> bool:
        """Blocks until ``predicate(stats)`` holds or ``timeout`` passes."""
        return self._gauge.wait_until(predicate, timeout)

    def close(self) -> None:
        """Does nothing: this bulkhead owns no threads."""


class ThreadPoolBulkhead:
    """Runs calls on a dedicated pool and waits a bounded time for each.

    At most ``workers`` calls run at once and ``queue_size`` more may wait;
    beyond that, calls are rejected. A caller that times out gets its thread
    back, and a call still queued is cancelled, but a call already running
    keeps its worker and its slot until it returns. So a hung dependency ends
    up holding every worker, after which this bulkhead rejects every call: it
    fails fast instead of letting the hang spread.
    """

    def __init__(
        self,
        name: str,
        workers: int,
        *,
        queue_size: int = 0,
        timeout: float | None = None,
    ) -> None:
        """Creates the bulkhead and its (lazily started) pool.

        Args:
            name: The dependency's name, for reports, errors and thread names.
            workers: Threads in the pool, so the most calls running at once.
            queue_size: Calls allowed to wait for a free worker.
            timeout: Seconds a caller waits for a result before abandoning
                the call. ``None`` waits as long as the call takes.

        Raises:
            ValueError: If any argument is out of range.
        """
        _check_int("workers", workers, 1, MAX_LIMIT)
        _check_int("queue_size", queue_size, 0, MAX_QUEUE)
        if timeout is not None:
            _check_seconds("timeout", timeout, allow_zero=False)
        self._name = name
        self._timeout = timeout
        self._capacity = workers + queue_size
        # A permit is held from submission until the task finishes or is
        # cancelled. ThreadPoolExecutor's own queue is unbounded; the permits
        # are what bound it.
        self._permits = threading.BoundedSemaphore(self._capacity)
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"bulkhead-{name}"
        )
        self._gauge = _Gauge()

    @property
    def name(self) -> str:
        """The dependency this bulkhead guards."""
        return self._name

    def call(self, fn: Callable[[], T]) -> T:
        """Runs ``fn`` on the pool and waits up to ``timeout`` for it.

        Raises:
            BulkheadFullError: If every worker and queue slot is taken.
            CallTimeoutError: If the result did not arrive in time. The call
                is cancelled if it had not started, and abandoned if it had.
        """
        if not self._permits.acquire(blocking=False):
            self._gauge.rejected()
            raise BulkheadFullError(
                f"{self._name}: all {self._capacity} slots are taken"
            )
        try:
            future: Future[T] = self._pool.submit(self._gauge.run, fn)
        except BaseException:
            self._permits.release()
            raise
        # Fires on completion and on cancellation, so no path leaks a permit.
        future.add_done_callback(lambda _: self._permits.release())
        self._gauge.accepted()
        try:
            return future.result(timeout=self._timeout)
        except TimeoutError:
            if future.done():
                # Either fn itself raised TimeoutError (a socket timeout, say)
                # or it finished just after the wait expired. Report what
                # actually happened rather than a bulkhead timeout.
                return future.result()
            future.cancel()  # Succeeds only if the call never started.
            self._gauge.timed_out()
            raise CallTimeoutError(
                f"{self._name}: no result within {self._timeout:g}s"
            ) from None

    def stats(self) -> Stats:
        """Returns a snapshot of the counters."""
        return self._gauge.snapshot()

    def wait_until(
        self, predicate: Callable[[Stats], bool], timeout: float | None
    ) -> bool:
        """Blocks until ``predicate(stats)`` holds or ``timeout`` passes."""
        return self._gauge.wait_until(predicate, timeout)

    def close(self) -> None:
        """Cancels queued calls and joins the pool's threads.

        This blocks until running calls return. A hung call must be ended
        first, for real by the dependency client's own I/O timeout.
        """
        self._pool.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> Self:
        """Returns the bulkhead itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Closes the bulkhead."""
        self.close()
