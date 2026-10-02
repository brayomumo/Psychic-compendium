"""Fan items in from producer processes to a bounded thread pool over pipes.

Each producer runs in its own process and owns one pipe. The calling process
is both supervisor and consumer: it multiplexes every pipe with
``multiprocessing.connection.wait`` and hands each item to a thread pool that
never holds more than ``max_in_flight`` items, so a slow pool pushes back
through the pipes onto the producers.

Everything sent through a pipe is pickled. Producers may only emit picklable
items; the handler runs in the calling process and may use anything.

POSIX only: shutdown relies on signal masks and SIGTERM.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import multiprocessing
import pickle
import signal
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from multiprocessing.reduction import ForkingPickler
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")

#: A producer: a picklable zero-argument callable returning an iterable.
#: Use a module-level function or ``functools.partial`` of one, because the
#: spawn and forkserver start methods pickle it to start the child.
Source = Callable[[], Iterable[T]]

LOG = logging.getLogger(__name__)

# Wire protocol. Every message is a tuple tagged with one of these.
_BATCH = 0  # (_BATCH, [item, ...])
_DONE = 1  # (_DONE,): the producer finished cleanly.
_FAILED = 2  # (_FAILED, formatted_traceback): the producer raised.

# What pickling raises for objects it cannot handle, depending on the type.
_PICKLE_ERRORS = (pickle.PicklingError, TypeError, AttributeError)
# Signals the parent coordinates; deferred while children start.
_COORDINATED_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})
_JOIN_TIMEOUT_S = 5.0
_LIVENESS_INTERVAL_S = 0.5
_MAX_ERRORS_KEPT = 10


class Status(enum.StrEnum):
    """How a producer's stream ended."""

    OK = "ok"
    #: The producer raised; its traceback is in ``ProducerOutcome.detail``.
    FAILED = "failed"
    #: It ended without an end-of-stream marker (killed, segfault, os._exit).
    CRASHED = "crashed"


@dataclass(frozen=True)
class ProducerOutcome:
    """What one producer delivered and how it ended.

    Attributes:
        name: The key the producer was registered under.
        status: How its stream ended.
        items: Items the consumer received from it and dispatched.
        exitcode: The process exit code; negative means killed by a signal.
            ``None`` for in-process sources, which have no process.
        detail: Traceback or diagnosis when the status is not OK.
    """

    name: str
    status: Status
    items: int
    exitcode: int | None
    detail: str = ""


@dataclass(frozen=True)
class RunReport:
    """The result of a completed run.

    Attributes:
        producers: One outcome per producer, in registration order.
        processed: Items the handler finished without raising.
        worker_failures: Items the handler raised on.
        worker_errors: The first few handler errors, so a handler that fails
            on every item cannot exhaust memory.
    """

    producers: tuple[ProducerOutcome, ...]
    processed: int
    worker_failures: int
    worker_errors: tuple[str, ...]

    @property
    def received(self) -> int:
        """Items received from all producers."""
        return sum(p.items for p in self.producers)

    @property
    def ok(self) -> bool:
        """True when every producer finished and every item was handled."""
        return self.worker_failures == 0 and all(
            p.status is Status.OK for p in self.producers
        )


class ProducerStartError(RuntimeError):
    """A producer process could not be started."""


class UnpicklableItemError(TypeError):
    """A producer yielded an item that cannot cross a process boundary."""


@dataclass(frozen=True)
class DispatchStats:
    """Counters from a closed ``BoundedDispatcher``.

    Attributes:
        processed: Items the handler finished without raising.
        failures: Items the handler raised on.
        cancelled: Items dropped before they started, by a cancelling close.
        errors: The first few handler errors.
    """

    processed: int
    failures: int
    cancelled: int
    errors: tuple[str, ...]


def resolve_max_in_flight(workers: int, max_in_flight: int | None) -> int:
    """Validates the pool limits and fills in the default in-flight bound.

    Args:
        workers: Number of worker threads.
        max_in_flight: Items allowed queued or running at once, or ``None``
            for twice the worker count (enough to keep workers busy while
            the feeder fetches the next item).

    Returns:
        The in-flight bound to use.

    Raises:
        ValueError: If either limit is below 1.
    """
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    if max_in_flight is None:
        return 2 * workers
    if max_in_flight < 1:
        raise ValueError(f"max_in_flight must be >= 1, got {max_in_flight}")
    return max_in_flight


class BoundedDispatcher(Generic[T]):
    """Runs ``handler(item)`` on a thread pool with bounded in-flight work.

    ``ThreadPoolExecutor`` queues without limit, so a fast feeder would buffer
    the whole stream in memory. ``submit`` blocks instead while
    ``max_in_flight`` items are queued or running, which turns a slow handler
    into backpressure on whoever is feeding it.
    """

    def __init__(
        self,
        handler: Callable[[T], object],
        *,
        workers: int,
        max_in_flight: int | None = None,
    ) -> None:
        """Creates the dispatcher. Worker threads start on first submit.

        Args:
            handler: Called once per item on a worker thread.
            workers: Number of worker threads.
            max_in_flight: See ``resolve_max_in_flight``.
        """
        bound = resolve_max_in_flight(workers, max_in_flight)
        self._handler = handler
        self._slots = threading.BoundedSemaphore(bound)
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="piper-worker"
        )
        self._lock = threading.Lock()
        self._processed = 0
        self._failures = 0
        self._cancelled = 0
        self._errors: list[str] = []

    def submit(self, item: T) -> None:
        """Queues ``item``, blocking while the in-flight bound is reached."""
        self._slots.acquire()
        try:
            future = self._executor.submit(self._handler, item)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda f: self._on_done(f, item))

    def _on_done(self, future: Future[object], item: T) -> None:
        try:
            with self._lock:
                if future.cancelled():
                    self._cancelled += 1
                elif (exc := future.exception()) is None:
                    self._processed += 1
                else:
                    self._failures += 1
                    if len(self._errors) < _MAX_ERRORS_KEPT:
                        self._errors.append(
                            f"{type(exc).__name__}: {exc} "
                            f"(item {_truncated_repr(item)})"
                        )
        finally:
            self._slots.release()

    def close(self, *, cancel_pending: bool = False) -> DispatchStats:
        """Waits for running items and stops the pool.

        Args:
            cancel_pending: Drop queued items that have not started yet.
                Running items always finish: threads cannot be killed.

        Returns:
            The final counters.
        """
        self._executor.shutdown(wait=True, cancel_futures=cancel_pending)
        with self._lock:
            return DispatchStats(
                self._processed,
                self._failures,
                self._cancelled,
                tuple(self._errors),
            )


def _truncated_repr(item: object, limit: int = 80) -> str:
    text = repr(item)
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class _Producer:
    """Parent-side bookkeeping for one producer process."""

    name: str
    process: BaseProcess
    reader: Connection
    items: int = 0
    status: Status = Status.CRASHED
    detail: str = "never reported"
    exitcode: int | None = None


def run_pipeline(  # noqa: PLR0913 - keyword-only tuning knobs.
    producers: Mapping[str, Source[T]],
    handler: Callable[[T], object],
    *,
    workers: int = 4,
    max_in_flight: int | None = None,
    batch_size: int = 32,
    start_method: str | None = None,
) -> RunReport:
    """Streams every producer's items through ``handler`` until all end.

    One producer failing or crashing does not stop the others; it is reported
    in the returned ``RunReport``. On any exception, including
    ``KeyboardInterrupt``, the producers are terminated, queued items are
    dropped, running items finish, and the exception propagates.

    Args:
        producers: Name to source. Each source runs in its own process and
            must be picklable under spawn and forkserver, as must every item
            it yields.
        handler: Called once per item on a worker thread in this process.
            Need not be picklable.
        workers: Number of worker threads.
        max_in_flight: Items queued or running at once; ``None`` for twice
            ``workers``.
        batch_size: Items per pipe message. Larger batches amortise the
            per-message cost; a crashed producer loses its unsent batch.
        start_method: ``"spawn"``, ``"fork"``, ``"forkserver"``, or ``None``
            for the platform default.

    Returns:
        A report with one outcome per producer, in registration order.

    Raises:
        ValueError: If a limit or the start method is invalid.
        ProducerStartError: If a producer could not be started.
    """
    resolve_max_in_flight(workers, max_in_flight)
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    context = multiprocessing.get_context(start_method)

    started: list[_Producer] = []
    clean_exit = False
    try:
        with _coordinated_signals_deferred():
            for name, source in producers.items():
                started.append(_start(context, name, source, batch_size))
        LOG.info(
            "started %d producers (%s)",
            len(started),
            context.get_start_method(),
        )
        # Created only after every fork: forking a process that already has
        # worker threads can deadlock the child on a lock held by one of them.
        dispatcher = BoundedDispatcher(
            handler, workers=workers, max_in_flight=max_in_flight
        )
        try:
            _consume(started, dispatcher)
            stats = dispatcher.close()
        except BaseException:
            _terminate(started)  # Stop the inflow, then drop queued work.
            dispatcher.close(cancel_pending=True)
            raise
        clean_exit = True
    finally:
        _reap(started, terminate=not clean_exit)

    return RunReport(
        producers=tuple(
            ProducerOutcome(p.name, p.status, p.items, p.exitcode, p.detail)
            for p in started
        ),
        processed=stats.processed,
        worker_failures=stats.failures,
        worker_errors=stats.errors,
    )


@contextlib.contextmanager
def _coordinated_signals_deferred() -> Iterator[None]:
    """Holds SIGINT and SIGTERM on this thread while producers start.

    Children inherit the starting thread's mask across fork and exec, so a
    Ctrl+C that lands mid-start stays pending in each child until
    ``_produce`` has installed its own dispositions, instead of killing the
    child during bootstrap with a traceback. The parent receives the signal
    as soon as the mask is restored. Masks are per thread: in a parent with
    other threads, one of them may take a process-wide signal at once. The
    caller's try/finally still reaps every started child in that case.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _COORDINATED_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _start(
    context: BaseContext, name: str, source: Source[Any], batch_size: int
) -> _Producer:
    reader, writer = context.Pipe(duplex=False)
    # typeshed declares Process only on the concrete context classes, but
    # every context that get_context() returns has it.
    process: BaseProcess = context.Process(  # type: ignore[attr-defined]
        target=_produce,
        args=(source, reader, writer, batch_size),
        name=f"piper-producer-{name}",
        # A second Ctrl+C can interrupt cleanup; daemonic children are then
        # still terminated at interpreter exit rather than waited for.
        daemon=True,
    )
    try:
        process.start()
    except Exception as exc:
        reader.close()
        hint = (
            " Under spawn and forkserver the source must be picklable: a "
            "module-level function, or functools.partial of one."
            if isinstance(exc, _PICKLE_ERRORS)
            else ""
        )
        raise ProducerStartError(
            f"could not start producer {name!r}: {exc}.{hint}"
        ) from exc
    finally:
        # The child has its own copy now. Keeping ours would mean EOF never
        # arrives, and a producer forked later would inherit this write end
        # and keep the pipe open after this producer died.
        writer.close()
    LOG.debug("producer %r is pid %s", name, process.pid)
    return _Producer(name, process, reader)


def _produce(
    source: Source[Any],
    reader: Connection,
    writer: Connection,
    batch_size: int,
) -> None:
    """Entry point of a producer process."""
    # Ctrl+C signals the whole foreground process group. The parent owns
    # shutdown and stops producers with SIGTERM, so ignore SIGINT here. The
    # parent deferred both signals while starting us; set dispositions first,
    # then unblock, so a pending SIGINT is discarded and a pending SIGTERM
    # (from an early terminate) takes effect.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # Fork copies handlers.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _COORDINATED_SIGNALS)
    # Hold no read end of our own pipe: if the parent dies, the next send
    # must fail with BrokenPipeError rather than block on a full pipe forever.
    reader.close()

    batch: list[Any] = []
    try:
        try:
            for item in source():
                batch.append(item)
                if len(batch) >= batch_size:
                    _send_batch(writer, batch)
            _send_batch(writer, batch)
        except Exception:
            failure = traceback.format_exc()
            # Best effort: deliver what was produced before the failure.
            with contextlib.suppress(Exception):
                _send_batch(writer, batch)
            writer.send((_FAILED, failure))
            raise SystemExit(1) from None
        writer.send((_DONE,))
    except BrokenPipeError:
        raise SystemExit(1) from None  # The consumer is gone; nobody to tell.
    finally:
        writer.close()


def _send_batch(writer: Connection, batch: list[Any]) -> None:
    """Sends and clears ``batch``.

    On an unpicklable item, delivers the items ahead of it, then raises, so
    the consumer always receives an exact prefix of the stream.

    Raises:
        UnpicklableItemError: If an item in the batch cannot be pickled.
    """
    if not batch:
        return
    try:
        writer.send((_BATCH, batch))
    except _PICKLE_ERRORS as exc:
        # send() pickles before writing, so nothing reached the pipe. Look
        # for the culprit outside this block, to keep tracebacks short.
        batch_error = exc
    else:
        batch.clear()
        return

    culprit = _first_unpicklable(batch)
    if culprit is None:
        raise batch_error  # Every item pickles alone; the fault is elsewhere.
    index, item_error = culprit
    kind = f"{type(batch[index]).__module__}.{type(batch[index]).__qualname__}"
    good = batch[:index]
    batch.clear()
    if good:
        writer.send((_BATCH, good))
    raise UnpicklableItemError(
        f"{kind} cannot be pickled, so it cannot cross a process boundary"
    ) from item_error


def _first_unpicklable(batch: list[Any]) -> tuple[int, Exception] | None:
    for index, item in enumerate(batch):
        try:
            ForkingPickler.dumps(item)
        except _PICKLE_ERRORS as exc:
            return index, exc
    return None


def _consume(
    producers: list[_Producer], dispatcher: BoundedDispatcher[Any]
) -> None:
    """Multiplexes every producer's pipe until all streams have ended.

    EOF reports a dead producer instantly, but only once every copy of its
    write end is closed, and a grandchild it forked may hold one. Process
    sentinels do not help: under fork and spawn they are pipes too, inherited
    the same way. So every ``_LIVENESS_INTERVAL_S`` the consumer also asks
    the kernel (``waitpid``, via ``is_alive``) which producers have exited.
    """
    open_streams = {p.reader: p for p in producers}
    next_check = time.monotonic() + _LIVENESS_INTERVAL_S
    while open_streams:
        for ready in wait(list(open_streams), timeout=_LIVENESS_INTERVAL_S):
            reader = cast(Connection, ready)  # We wait on nothing else.
            if not _receive(open_streams[reader], dispatcher):
                del open_streams[reader]
        if time.monotonic() >= next_check:
            for reader, producer in list(open_streams.items()):
                if not producer.process.is_alive():
                    del open_streams[reader]
                    _drain_exited(producer, dispatcher)
            next_check = time.monotonic() + _LIVENESS_INTERVAL_S


def _receive(producer: _Producer, dispatcher: BoundedDispatcher[Any]) -> bool:
    """Handles one message. Returns False once the stream has ended."""
    try:
        message = producer.reader.recv()
    except EOFError:
        _finish(producer, Status.CRASHED, "pipe closed without end-of-stream")
        return False
    except Exception as exc:  # The bytes arrived but would not unpickle.
        producer.process.terminate()
        _finish(
            producer,
            Status.FAILED,
            f"consumer could not unpickle a message: {exc!r}",
        )
        return False

    tag = message[0]
    if tag == _BATCH:
        for item in message[1]:
            dispatcher.submit(item)
            producer.items += 1
        return True
    if tag == _DONE:
        _finish(producer, Status.OK, "")
    elif tag == _FAILED:
        _finish(producer, Status.FAILED, message[1])
    else:
        producer.process.terminate()
        _finish(producer, Status.FAILED, f"unknown message tag {tag!r}")
    return False


def _drain_exited(
    producer: _Producer, dispatcher: BoundedDispatcher[Any]
) -> None:
    """Reads what an exited producer left in its pipe, then classifies it.

    A process's writes are in the pipe by the time it has exited, so
    ``poll(0)`` sees all of them. If neither an end-of-stream marker nor EOF
    turns up, another process still holds the write end and blocking on the
    pipe would hang.
    """
    while producer.reader.poll(0):
        if not _receive(producer, dispatcher):
            return
    _finish(producer, Status.CRASHED, "exited without end-of-stream")


def _finish(producer: _Producer, status: Status, detail: str) -> None:
    producer.status = status
    producer.detail = detail
    producer.reader.close()


def _terminate(producers: list[_Producer]) -> None:
    for producer in producers:
        if producer.process.is_alive():
            producer.process.terminate()


def _reap(producers: list[_Producer], *, terminate: bool) -> None:
    """Joins every producer, killing any that will not exit.

    All producers share one grace period, so shutdown takes at most
    ``_JOIN_TIMEOUT_S`` however many are stuck. Also records each exit code
    and folds it into a crash diagnosis.
    """
    if terminate:
        _terminate(producers)
    deadline = time.monotonic() + _JOIN_TIMEOUT_S
    for producer in producers:
        producer.process.join(max(0.0, deadline - time.monotonic()))
        if producer.process.exitcode is None:  # Wedged, or ignoring SIGTERM.
            LOG.warning("killing producer %r", producer.name)
            producer.process.kill()
            producer.process.join()
        producer.exitcode = producer.process.exitcode
        if producer.status is Status.CRASHED:
            producer.detail += f" ({describe_exit(producer.exitcode)})"
        producer.reader.close()
        producer.process.close()


def describe_exit(exitcode: int | None) -> str:
    """Describes a process exit code for humans.

    Args:
        exitcode: As reported by ``multiprocessing.Process.exitcode``.

    Returns:
        For example ``"exit code 1"`` or ``"killed by SIGKILL"``.
    """
    if exitcode is None:
        return "still running"
    if exitcode < 0:
        try:
            return f"killed by {signal.Signals(-exitcode).name}"
        except ValueError:
            return f"killed by signal {-exitcode}"
    return f"exit code {exitcode}"
