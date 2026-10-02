"""A simulated service whose requests call one of several dependencies.

Requests run on one shared pool of request workers, as in any web server.
Each request calls a ``Dependency`` through the ``Isolation`` chosen by the
``Variant``, so the same load can be replayed with and without bulkheads.

``Dependency`` simulates a downstream service whose behaviour can be switched
at runtime (healthy, slow, hung or failing). A hung dependency blocks on an
event that only ``release()`` or ``shutdown()`` sets, which lets tests create
a hang that lasts exactly as long as they want, with no timing involved.
"""

import enum
import math
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from bulkhead import (
    MAX_LIMIT,
    MAX_QUEUE,
    MAX_TIMEOUT_S,
    BulkheadFullError,
    CallTimeoutError,
    Isolation,
    SemaphoreBulkhead,
    Stats,
    ThreadPoolBulkhead,
    Unisolated,
)

__all__ = [
    "Dependency",
    "DependencyError",
    "DependencyStats",
    "Mode",
    "Outcome",
    "Result",
    "Service",
    "ServiceConfig",
    "Variant",
]


class Mode(enum.StrEnum):
    """How a simulated dependency behaves."""

    HEALTHY = "healthy"  # Answers after its normal latency.
    SLOW = "slow"  # Answers after a longer, configurable latency.
    HUNG = "hung"  # Never answers until released.
    FAILING = "failing"  # Raises immediately.


class DependencyError(RuntimeError):
    """The simulated dependency failed."""


@dataclass(frozen=True)
class DependencyStats:
    """A snapshot of a dependency's counters."""

    started: int
    completed: int
    in_flight: int
    peak_in_flight: int


class Dependency:
    """A downstream service whose behaviour can be controlled at runtime."""

    def __init__(self, name: str, latency: float = 0.0) -> None:
        """Creates a healthy dependency.

        Args:
            name: Used in results and messages.
            latency: Seconds a healthy call takes (simulated work).

        Raises:
            ValueError: If ``latency`` is negative or not finite.
        """
        _check_latency(latency)
        self.name = name
        self._cond = threading.Condition()
        self._mode = Mode.HEALTHY
        self._healthy_latency = latency
        self._slow_latency = latency
        # A fresh gate per hang. A call reads the gate under the lock and waits
        # on it after releasing the lock; with a single reused Event, a call
        # caught between those two steps by release() then hang() would block
        # on the new hang. With a fresh gate it waits on the one that was set.
        self._gate = threading.Event()
        # Set once, by shutdown(): cuts every simulated wait short.
        self._closed = threading.Event()
        self._started = 0
        self._completed = 0
        self._in_flight = 0
        self._peak = 0

    @property
    def mode(self) -> Mode:
        """The current behaviour."""
        with self._cond:
            return self._mode

    def hang(self) -> None:
        """Makes every call block until ``release()`` or ``shutdown()``."""
        with self._cond:
            self._mode = Mode.HUNG
            self._gate = threading.Event()

    def slow(self, latency: float) -> None:
        """Makes every call take ``latency`` seconds."""
        _check_latency(latency)
        with self._cond:
            self._mode = Mode.SLOW
            self._slow_latency = latency

    def fail(self) -> None:
        """Makes every call raise ``DependencyError`` immediately."""
        with self._cond:
            self._mode = Mode.FAILING

    def release(self) -> None:
        """Ends a hang and returns the dependency to healthy."""
        with self._cond:
            self._mode = Mode.HEALTHY
            self._gate.set()

    def shutdown(self) -> None:
        """Releases every blocked or sleeping call, now and in the future."""
        self._closed.set()
        self.release()

    def call(self) -> str:
        """Performs one call according to the current mode.

        Returns:
            A short acknowledgement.

        Raises:
            DependencyError: In ``FAILING`` mode.
        """
        with self._cond:
            self._started += 1
            self._in_flight += 1
            self._peak = max(self._peak, self._in_flight)
            mode, gate = self._mode, self._gate
            latency = (
                self._slow_latency
                if mode is Mode.SLOW
                else self._healthy_latency
            )
            self._cond.notify_all()
        try:
            if mode is Mode.FAILING:
                raise DependencyError(f"{self.name} is failing")
            if mode is Mode.HUNG:
                gate.wait()
            elif latency > 0:
                self._closed.wait(latency)  # Simulated work.
            return f"{self.name}: ok"
        finally:
            with self._cond:
                self._in_flight -= 1
                self._completed += 1
                self._cond.notify_all()

    def stats(self) -> DependencyStats:
        """Returns a snapshot of the counters."""
        with self._cond:
            return DependencyStats(
                self._started, self._completed, self._in_flight, self._peak
            )

    def wait_until(
        self, predicate: Callable[[DependencyStats], bool], timeout: float
    ) -> bool:
        """Blocks until ``predicate(stats)`` holds or ``timeout`` passes.

        Returns:
            Whether the predicate held before the timeout.
        """
        with self._cond:
            return self._cond.wait_for(
                lambda: predicate(
                    DependencyStats(
                        self._started,
                        self._completed,
                        self._in_flight,
                        self._peak,
                    )
                ),
                timeout,
            )


def _check_latency(latency: float) -> None:
    if not math.isfinite(latency) or not 0 <= latency <= MAX_TIMEOUT_S:
        raise ValueError(
            f"latency must be in 0..{MAX_TIMEOUT_S:g} seconds, got {latency}"
        )


class Variant(enum.StrEnum):
    """How the service isolates its dependencies."""

    SHARED = "shared"  # No bulkheads: every call holds a request worker.
    SEMAPHORE = "semaphore"  # A semaphore bulkhead per dependency.
    THREAD_POOL = "thread-pool"  # A thread-pool bulkhead per dependency.


@dataclass(frozen=True)
class ServiceConfig:
    """Sizes and timeouts for a ``Service``.

    Attributes:
        workers: Request workers, shared by requests to every dependency.
        limit: Per-dependency bulkhead capacity (semaphore slots, or pool
            workers).
        queue_size: Calls that may wait for a thread-pool bulkhead worker.
        timeout: Seconds a request waits on a thread-pool bulkhead call.
        max_wait: Seconds a request waits for a semaphore bulkhead slot.
    """

    workers: int = 8
    limit: int = 2
    queue_size: int = 0
    timeout: float = 0.1
    max_wait: float = 0.0

    def validate(self) -> None:
        """Checks every field, reporting all problems at once.

        Raises:
            ValueError: Listing every out-of-range field.
        """
        problems = []
        if not 1 <= self.workers <= MAX_LIMIT:
            problems.append(f"workers must be in 1..{MAX_LIMIT}")
        if not 1 <= self.limit <= MAX_LIMIT:
            problems.append(f"limit must be in 1..{MAX_LIMIT}")
        if not 0 <= self.queue_size <= MAX_QUEUE:
            problems.append(f"queue_size must be in 0..{MAX_QUEUE}")
        if not (math.isfinite(self.timeout) and self.timeout > 0):
            problems.append("timeout must be a positive number of seconds")
        if not (math.isfinite(self.max_wait) and self.max_wait >= 0):
            problems.append("max_wait must be a non-negative number")
        if problems:
            raise ValueError("; ".join(problems))


def make_isolation(
    variant: Variant, name: str, config: ServiceConfig
) -> Isolation:
    """Builds the isolation ``variant`` prescribes for dependency ``name``."""
    if variant is Variant.SHARED:
        return Unisolated(name)
    if variant is Variant.SEMAPHORE:
        return SemaphoreBulkhead(name, config.limit, max_wait=config.max_wait)
    return ThreadPoolBulkhead(
        name,
        config.limit,
        queue_size=config.queue_size,
        timeout=config.timeout,
    )


class Outcome(enum.StrEnum):
    """What happened to one request."""

    OK = "ok"
    REJECTED = "rejected"  # The bulkhead was full; the call was not made.
    TIMED_OUT = "timed out"  # The bulkhead's timeout expired.
    FAILED = "failed"  # The dependency raised.


@dataclass(frozen=True)
class Result:
    """The outcome and timing of one request.

    Attributes:
        dependency: Which dependency the request called.
        outcome: What happened.
        queued_s: Seconds spent waiting for a free request worker.
        service_s: Seconds spent on the request worker.
    """

    dependency: str
    outcome: Outcome
    queued_s: float
    service_s: float

    @property
    def total_s(self) -> float:
        """Seconds from submission to completion."""
        return self.queued_s + self.service_s


class Service:
    """Handles requests on a shared worker pool, isolating each dependency.

    The service owns its request workers and its isolations. It does not own
    the dependencies: a hung dependency must be released (in production, by
    the client's own I/O timeout) before ``close()`` can join the threads
    stuck inside it.
    """

    def __init__(
        self,
        variant: Variant,
        dependencies: Mapping[str, Dependency],
        config: ServiceConfig,
    ) -> None:
        """Creates the service. Threads start lazily, on first use.

        Args:
            variant: How to isolate each dependency.
            dependencies: The dependencies requests may call, by name.
            config: Pool sizes and timeouts.

        Raises:
            ValueError: If ``config`` is invalid or there are no dependencies.
        """
        config.validate()
        if not dependencies:
            raise ValueError("a service needs at least one dependency")
        self.variant = variant
        self._dependencies = dict(dependencies)
        self._isolations = {
            name: make_isolation(variant, name, config)
            for name in self._dependencies
        }
        self._requests = ThreadPoolExecutor(
            max_workers=config.workers, thread_name_prefix="request"
        )

    def submit(self, dependency: str) -> Future[Result]:
        """Queues one request to ``dependency`` on the request workers.

        Raises:
            ValueError: If ``dependency`` is unknown.
        """
        if dependency not in self._dependencies:
            raise ValueError(f"unknown dependency {dependency!r}")
        return self._requests.submit(
            self._handle, dependency, time.perf_counter()
        )

    def isolation(self, dependency: str) -> Isolation:
        """Returns the isolation guarding ``dependency``."""
        return self._isolations[dependency]

    def stats(self, dependency: str) -> Stats:
        """Returns the counters of the isolation guarding ``dependency``."""
        return self._isolations[dependency].stats()

    def _handle(self, name: str, submitted: float) -> Result:
        started = time.perf_counter()
        try:
            self._isolations[name].call(self._dependencies[name].call)
            outcome = Outcome.OK
        except BulkheadFullError:
            outcome = Outcome.REJECTED
        except CallTimeoutError:
            outcome = Outcome.TIMED_OUT
        except Exception:  # Any dependency error is a FAILED outcome.
            outcome = Outcome.FAILED
        return Result(
            name, outcome, started - submitted, time.perf_counter() - started
        )

    def close(self) -> None:
        """Drops queued requests, then joins every thread the service owns.

        Blocks while any request or pool worker is inside a hung dependency.
        """
        self._requests.shutdown(wait=True, cancel_futures=True)
        for isolation in self._isolations.values():
            isolation.close()

    def __enter__(self) -> Self:
        """Returns the service itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Closes the service."""
        self.close()
