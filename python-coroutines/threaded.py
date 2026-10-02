"""Push values from a coroutine pipeline into a worker thread.

``threaded(target)`` is David Beazley's coroutine-to-thread bridge: a coroutine
that accepts values via ``send()`` and hands them, in order, to ``target``
running in its own thread, through a bounded queue. The sender carries on as
soon as an item is queued, so a *blocking* target (file or network I/O, or a C
extension that releases the GIL) overlaps with the producer.

Threads do not make Python bytecode run in parallel on the default build. The
GIL lets one thread execute it at a time, so a CPU-bound target gains nothing;
the demo measures both cases. Only a free-threaded build (PEP 703, 3.13+) runs
the producer and a CPU-bound target in parallel.

Run ``python3 threaded.py --help`` for options.
"""

import argparse
import contextlib
import enum
import inspect
import itertools
import logging
import queue
import sys
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from typing import Final, Generic, TypeVar

import cli
from pipeline import collect, feed, take
from prime import Sink, coroutine

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAXSIZE: Final = 64
MAX_MAXSIZE: Final = 100_000
MAX_ITEMS: Final = 1_000_000
MAX_ITEM_SECONDS: Final = 60.0

# How often a blocked side re-checks that the other side still exists. This
# bounds how fast an abandoned bridge is noticed; items never wait on it.
_LIVENESS_SECONDS: Final = 0.05

_thread_ids = itertools.count(1)


class _Signal(enum.Enum):
    """Private control markers. Callers cannot send them by accident."""

    END = enum.auto()  # queued by close(): deliver what is queued, then stop
    QUIT = enum.auto()  # seen by the worker: stop now, discard the backlog


class _Relay(Generic[T]):
    """The thread side of one bridge. It owns ``target`` once started.

    After the target finishes or fails, the worker keeps draining the queue
    until ``END``, discarding items, so a sender can never block forever on a
    full queue; the sender learns why on its next ``send()`` or on
    ``close()``. An abort makes the worker stop after its current item.
    """

    def __init__(self, target: Sink[T], maxsize: int, name: str) -> None:
        self._target = target
        self._inbox: queue.Queue[T | _Signal] = queue.Queue(maxsize)
        self._abort = threading.Event()
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._error_reported = False
        self._target_done = False
        self.thread = threading.Thread(target=self._run, name=name)

    # --- worker thread ------------------------------------------------------

    def _run(self) -> None:
        live = True
        try:
            while True:
                item = self._next()
                if item is _Signal.QUIT:
                    return
                try:
                    if item is _Signal.END:
                        return
                    if live:
                        live = self._deliver(item)
                finally:
                    # Keeps Queue.join() meaningful: every item is accounted
                    # for once it is delivered or discarded.
                    self._inbox.task_done()
        finally:
            if live:
                self._close_target()

    def _next(self) -> T | _Signal:
        while not self._abort.is_set():
            try:
                return self._inbox.get(timeout=_LIVENESS_SECONDS)
            except queue.Empty:
                # Interpreter shutdown joins non-daemon threads, and stops the
                # main thread first. An owner that never called close() must
                # not make the process hang (the first version did).
                if not threading.main_thread().is_alive():
                    break
        return _Signal.QUIT

    def _deliver(self, item: T) -> bool:
        try:
            self._target.send(item)
        except StopIteration:
            self._record(None)
            return False
        except BaseException as exc:  # handed to the sender, never lost
            self._record(exc)
            return False
        return True

    def _close_target(self) -> None:
        try:
            self._target.close()
        except BaseException as exc:  # handed to the sender, never lost
            self._record(exc)

    def _record(self, error: BaseException | None) -> None:
        with self._lock:
            self._target_done = True
            if error is not None and self._error is None:
                self._error = error

    # --- sender thread ------------------------------------------------------

    def on_worker_thread(self) -> bool:
        """Whether the caller is the worker, i.e. the target itself."""
        return threading.current_thread() is self.thread

    def accepting(self) -> bool:
        """Check the worker before queueing another item.

        Returns:
            ``False`` if the target finished on its own.

        Raises:
            BaseException: The target's exception, the first time it is seen.
            RuntimeError: The worker stopped without being told to
                (interpreter shutdown).
        """
        error = self.take_error()
        if error is not None:
            raise error
        with self._lock:
            if self._target_done:
                return False
        if not self.thread.is_alive():
            raise RuntimeError(
                f"worker thread {self.thread.name!r} stopped before close(); "
                "the interpreter is shutting down"
            )
        return True

    def put(self, item: T) -> None:
        """Queue ``item``, waiting while the queue is full (backpressure).

        Raises:
            RuntimeError: The worker thread stopped while we waited.
        """
        if not self._offer(item):
            raise RuntimeError(f"worker thread {self.thread.name!r} stopped")

    def stop(self, *, drain: bool) -> None:
        """Tell the worker to finish, then join it.

        Args:
            drain: Deliver every queued item first (``close()``), or discard
                the backlog and stop after the item in progress (an exception
                such as Ctrl+C).
        """
        try:
            if drain:
                self._offer(_Signal.END)
            else:
                self.abort()
            self.thread.join()
        except BaseException:
            # A second Ctrl+C while waiting: stop delivering the backlog too.
            self.abort()
            raise

    def abort(self) -> None:
        """Make the worker stop after its current item, discarding the rest."""
        self._abort.set()
        # Wake a worker idling in get(); if the queue is full it is busy and
        # will see the flag when it finishes the current item.
        with contextlib.suppress(queue.Full):
            self._inbox.put_nowait(_Signal.END)

    def take_error(self) -> BaseException | None:
        """Return the target's exception once; later calls return ``None``."""
        with self._lock:
            if self._error is None or self._error_reported:
                return None
            self._error_reported = True
            error = self._error
        error.add_note(f"raised by the target in thread {self.thread.name!r}")
        return error

    def _offer(self, item: T | _Signal) -> bool:
        while True:
            try:
                self._inbox.put(item, timeout=_LIVENESS_SECONDS)
            except queue.Full:
                if not self.thread.is_alive():
                    return False
            else:
                return True


def _check_arguments(target: Sink[T], maxsize: int) -> None:
    problem = None
    if not 1 <= maxsize <= MAX_MAXSIZE:
        problem = f"maxsize must be in 1..{MAX_MAXSIZE}, got {maxsize}"
    elif inspect.getgeneratorstate(target) != inspect.GEN_SUSPENDED:
        problem = (
            "target must be a primed coroutine suspended at a yield, but it is "
            f"{inspect.getgeneratorstate(target)}"
        )
    if problem is not None:
        target.close()  # the bridge owns its target, even when refusing it
        raise ValueError(problem)


@coroutine
def threaded(
    target: Sink[T], *, maxsize: int = DEFAULT_MAXSIZE, name: str | None = None
) -> Generator[None, T, None]:
    """Deliver every value sent in to ``target``, in order, on a worker thread.

    The bridge owns ``target`` and its thread:

    * ``send()`` queues the item and returns. It waits only while ``maxsize``
      items are already queued (backpressure).
    * If the target raises, the exception is re-raised by the next ``send()``
      (and that item is not accepted) or by ``close()``, exactly once.
    * If the target finishes on its own, the next ``send()`` raises
      ``StopIteration``, like any finished coroutine, so ``forward()`` and
      ``feed()`` stop cleanly.
    * ``close()`` delivers every accepted item, closes the target and joins
      the thread. Any other exception thrown in (Ctrl+C included) discards the
      backlog, joins the thread after its current item, and propagates.
    * Once finished, ``send()`` raises ``StopIteration``.

    Only one thread may send to the bridge at a time, and nothing else may
    touch ``target`` once it is handed over.

    Args:
        target: A primed coroutine; it runs on the worker thread.
        maxsize: Most items waiting in the queue, 1 to ``MAX_MAXSIZE``.
        name: Worker thread name; defaults to ``threaded-<n>``.

    Raises:
        ValueError: ``maxsize`` is out of range or ``target`` is not primed.
            ``target`` is closed in that case too.
    """
    _check_arguments(target, maxsize)
    relay = _Relay(target, maxsize, name or f"threaded-{next(_thread_ids)}")
    relay.thread.start()
    clean = False
    try:
        while True:
            item = yield
            if relay.on_worker_thread():
                break  # the target is sending to its own bridge; see finally
            if not relay.accepting():
                clean = True
                return
            relay.put(item)
    except GeneratorExit:
        clean = True
        raise
    finally:
        if relay.on_worker_thread():
            # Waiting for a free slot or for join() here would mean the worker
            # waiting on itself: a deadlock. Stop it instead, and say why.
            relay.abort()
            logger.error(
                "target in thread %r used its own bridge; bridge stopped",
                relay.thread.name,
            )
            raise RuntimeError(
                "a target cannot send to or close its own bridge"
            )
        relay.stop(drain=clean)
        error = relay.take_error()
        if error is not None:
            if not clean:
                # Another exception is already propagating; keep it, and log
                # this one rather than lose it.
                logger.error(
                    "target in thread %r also failed",
                    relay.thread.name,
                    exc_info=error,
                )
            else:
                raise error from None


@contextlib.contextmanager
def in_thread(
    target: Sink[T], *, maxsize: int = DEFAULT_MAXSIZE, name: str | None = None
) -> Iterator[Sink[T]]:
    """Run ``target`` behind a :func:`threaded` bridge for a ``with`` block.

    Leaving the block normally closes the bridge: every accepted item is
    delivered, the thread is joined, and an unreported target error is
    raised. Leaving it with an exception (Ctrl+C included) aborts the bridge
    instead: the backlog is discarded, the thread is joined after its current
    item, and the exception continues.

    Args:
        target: A primed coroutine; it runs on the worker thread.
        maxsize: Most items waiting in the queue.
        name: Worker thread name.

    Yields:
        The bridge, ready for ``send()``.
    """
    bridge = threaded(target, maxsize=maxsize, name=name)
    try:
        yield bridge
    except BaseException as exc:
        bridge.throw(exc)  # aborts the bridge, then re-raises exc
        raise
    bridge.close()


# --- Demo -------------------------------------------------------------------


def _work(seconds: float, *, cpu_bound: bool) -> None:
    if not cpu_bound:
        time.sleep(seconds)  # stands in for blocking I/O; releases the GIL
        return
    # Spin for `seconds` of this thread's CPU time. A wall-clock deadline would
    # keep running while the thread waits for the GIL and fake an overlap.
    deadline = time.thread_time() + seconds
    while time.thread_time() < deadline:
        pass


def produce(count: int, seconds: float, *, cpu_bound: bool) -> Iterator[int]:
    """Yield ``0..count-1``, doing ``seconds`` of work before each item.

    Args:
        count: Number of items.
        seconds: Work per item.
        cpu_bound: Spin the CPU instead of sleeping.

    Yields:
        The item numbers, in order.
    """
    for item in range(count):
        _work(seconds, cpu_bound=cpu_bound)
        yield item


@coroutine
def slow_recorder(
    into: list[int], seconds: float, *, cpu_bound: bool
) -> Generator[None, int, None]:
    """Spend ``seconds`` on each item received, then append it to ``into``.

    Args:
        into: Receives the items in arrival order.
        seconds: Work per item.
        cpu_bound: Spin the CPU instead of sleeping.
    """
    while True:
        item = yield
        _work(seconds, cpu_bound=cpu_bound)
        into.append(item)


@coroutine
def fails_on(bad: int) -> Generator[None, int, None]:
    """Raise ``ValueError`` when ``bad`` arrives.

    Args:
        bad: The item that triggers the failure.

    Raises:
        ValueError: On receiving ``bad``.
    """
    while True:
        if (yield) == bad:
            raise ValueError(f"cannot handle {bad}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse and validate the demo's command line.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        The parsed options. Invalid input exits with status 2.
    """
    parser = argparse.ArgumentParser(
        description="Overlap a producer with a target on a worker thread."
    )
    parser.add_argument(
        "--items",
        type=cli.bounded_int(0, MAX_ITEMS),
        default=8,
        help="default 8",
    )
    parser.add_argument(
        "--item-seconds",
        type=cli.bounded_seconds(MAX_ITEM_SECONDS),
        default=0.025,
        help="work to produce and to consume each item, default 0.025",
    )
    parser.add_argument(
        "--maxsize",
        type=cli.bounded_int(1, MAX_MAXSIZE),
        default=DEFAULT_MAXSIZE,
        help=f"bridge queue bound, default {DEFAULT_MAXSIZE}",
    )
    return parser.parse_args(argv)


def _compare(items: int, seconds: float, maxsize: int, cpu_bound: bool) -> str:
    inline: list[int] = []
    start = time.perf_counter()
    feed(
        produce(items, seconds, cpu_bound=cpu_bound),
        slow_recorder(inline, seconds, cpu_bound=cpu_bound),
    )
    inline_seconds = time.perf_counter() - start

    bridged: list[int] = []
    start = time.perf_counter()
    target = slow_recorder(bridged, seconds, cpu_bound=cpu_bound)
    with in_thread(target, maxsize=maxsize) as sink:
        for item in produce(items, seconds, cpu_bound=cpu_bound):
            sink.send(item)
    bridged_seconds = time.perf_counter() - start

    in_order = bridged == inline == list(range(items))
    return (
        f"inline {inline_seconds:6.3f}s  threaded {bridged_seconds:6.3f}s  "
        f"in order: {in_order}"
    )


def _run_demo(items: int, seconds: float, maxsize: int) -> None:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(
        f"{items} items, {seconds:g}s to produce and {seconds:g}s to consume "
        f"each, GIL enabled: {gil}"
    )
    print(f"  blocking target   {_compare(items, seconds, maxsize, False)}")
    print(f"  CPU-bound target  {_compare(items, seconds, maxsize, True)}")

    try:
        with in_thread(fails_on(3), maxsize=2, name="fragile") as sink:
            for item in range(10):
                sink.send(item)
    except ValueError as exc:
        print(f"  target error reached the producer: {exc!r} {exc.__notes__}")

    taken: list[int] = []
    feed(itertools.count(), threaded(take(3, collect(taken))))
    print(f"  take(3) on the worker stopped an infinite source at {taken}")
    print(f"  worker threads still running: {threading.active_count() - 1}")


def main(argv: Sequence[str] | None = None) -> int:
    """Time a producer feeding a slow target inline and through the bridge.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        0 on success, 130 after Ctrl+C, 143 after SIGTERM. Usage errors exit
        with status 2 from argparse.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    return cli.run_interruptible(
        lambda: _run_demo(args.items, args.item_seconds, args.maxsize),
        on_interrupt="interrupted; worker thread joined",
    )


if __name__ == "__main__":
    raise SystemExit(main())
