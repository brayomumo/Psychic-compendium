"""Tests for the coroutine-to-thread bridge.

Synchronisation is structural: events set by the code under test, and
``Queue.join()`` on an instrumented queue. Timeouts appear only as guards
that turn a deadlock into a failure.
"""

import contextlib
import faulthandler
import gc
import inspect
import io
import itertools
import os
import pathlib
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import unittest
from collections.abc import Generator, Iterator
from typing import Any
from unittest import mock

import threaded
from pipeline import collect, feed, take
from prime import Sink, coroutine

GUARD_SECONDS = 10  # waits inside targets; only reached on a bug
WATCHDOG_SECONDS = 30  # per test; only reached on a deadlock
PROTOTYPE_DIR = pathlib.Path(__file__).resolve().parent.parent


class RecordingQueue(queue.Queue[Any]):
    """A Queue that records its peak depth and any put that found it full."""

    def __init__(
        self, maxsize: int = 0, on_full: threading.Event | None = None
    ) -> None:
        super().__init__(maxsize)
        self.peak = 0
        self.on_full = on_full or threading.Event()

    def put(
        self, item: Any, block: bool = True, timeout: float | None = None
    ) -> None:
        if self.full():
            self.on_full.set()
        super().put(item, block, timeout)

    def _put(self, item: Any) -> None:  # called with the queue's mutex held
        super()._put(item)
        self.peak = max(self.peak, self._qsize())


@contextlib.contextmanager
def recorded_queues(
    on_full: threading.Event | None = None,
) -> Iterator[list[RecordingQueue]]:
    """Make every bridge created inside the block use a RecordingQueue."""
    created: list[RecordingQueue] = []

    def make(maxsize: int = 0) -> RecordingQueue:
        created.append(RecordingQueue(maxsize, on_full))
        return created[-1]

    with mock.patch.object(queue, "Queue", make):
        yield created


@coroutine
def recorder(into: list[Any]) -> Generator[None, Any, None]:
    """Append every item to ``into``."""
    while True:
        into.append((yield))


@coroutine
def held(
    into: list[int],
    release: threading.Event,
    holding: threading.Event | None = None,
) -> Generator[None, int, None]:
    """Hold the first item until ``release`` is set, then record everything."""
    first = yield
    if holding is not None:
        holding.set()
    if not release.wait(GUARD_SECONDS):
        raise AssertionError("target was never released")
    into.append(first)
    while True:
        into.append((yield))


@coroutine
def fails_on(bad: int, into: list[int]) -> Generator[None, int, None]:
    """Record items until ``bad`` arrives, then raise."""
    while True:
        item = yield
        if item == bad:
            raise ValueError(f"cannot handle {item}")
        into.append(item)


def worker_names() -> set[str]:
    return {t.name for t in threading.enumerate()}


class ThreadedTestCase(unittest.TestCase):
    """Leaves the threads it found; a deadlock fails the run, never hangs it.

    A join() that never returns cannot be timed out from inside the code
    under test, so faulthandler aborts the process with every thread's
    traceback if a test overruns its watchdog.
    """

    def setUp(self) -> None:
        self.baseline = threading.active_count()
        faulthandler.dump_traceback_later(WATCHDOG_SECONDS, exit=True)

    def tearDown(self) -> None:
        faulthandler.cancel_dump_traceback_later()
        self.assertEqual(threading.active_count(), self.baseline)


class DeliveryTest(ThreadedTestCase):
    def test_items_arrive_in_order(self) -> None:
        got: list[int] = []
        bridge = threaded.threaded(recorder(got), maxsize=8)
        for item in range(2000):
            bridge.send(item)
        bridge.close()
        self.assertEqual(got, list(range(2000)))

    def test_sentinel_as_class_bug_generator_exit_is_just_data(self) -> None:
        # The first version used the GeneratorExit class as its in-band stop
        # value, so sending it as data silently ended delivery.
        got: list[Any] = []
        bridge = threaded.threaded(recorder(got))
        for item in [1, GeneratorExit, 2, None, 3]:
            bridge.send(item)
        bridge.close()
        self.assertEqual(got, [1, GeneratorExit, 2, None, 3])

    def test_runs_target_on_named_worker_thread(self) -> None:
        seen: list[str] = []

        @coroutine
        def where() -> Generator[None, int, None]:
            while True:
                yield
                seen.append(threading.current_thread().name)

        bridge = threaded.threaded(where(), name="bridge-under-test")
        self.assertIn("bridge-under-test", worker_names())
        bridge.send(1)
        bridge.close()
        self.assertEqual(seen, ["bridge-under-test"])
        self.assertNotIn("bridge-under-test", worker_names())

    def test_default_thread_names_are_unique(self) -> None:
        bridges = [threaded.threaded(recorder([])) for _ in range(3)]
        names = {n for n in worker_names() if n.startswith("threaded-")}
        self.assertGreaterEqual(len(names), 3)
        for bridge in bridges:
            bridge.close()

    def test_composes_with_pipeline_feed(self) -> None:
        got: list[int] = []
        feed(range(100), threaded.threaded(recorder(got), maxsize=2))
        self.assertEqual(got, list(range(100)))


class BackpressureTest(ThreadedTestCase):
    def test_queue_depth_never_exceeds_maxsize(self) -> None:
        # The target holds item 0 until a put finds the queue full, so the
        # bound is reached; with an unbounded queue no put would ever find it
        # full and the target's guard would fail the test.
        release = threading.Event()
        got: list[int] = []
        with recorded_queues(on_full=release) as queues:
            bridge = threaded.threaded(held(got, release), maxsize=4)
            for item in range(50):
                bridge.send(item)
            bridge.close()
        self.assertEqual(got, list(range(50)))
        self.assertEqual(queues[0].maxsize, 4)
        self.assertEqual(queues[0].peak, 4)


class ShutdownTest(ThreadedTestCase):
    def test_never_joined_thread_bug_close_joins_worker(self) -> None:
        # The first version started a non-daemon thread and kept no handle
        # to it, so close() could return while the thread still ran.
        bridge = threaded.threaded(recorder([]), name="join-me")
        self.assertIn("join-me", worker_names())
        bridge.send(1)
        bridge.close()
        self.assertNotIn("join-me", worker_names())

    def test_never_joined_thread_bug_unclosed_bridge_does_not_hang_exit(
        self,
    ) -> None:
        # The first version hung the interpreter at exit when a bridge held
        # in a module global was never closed.
        script = textwrap.dedent(
            """
            import threaded
            from prime import coroutine

            @coroutine
            def report():
                got = []
                try:
                    while True:
                        got.append((yield))
                finally:
                    print("target closed after", got, flush=True)

            bridge = threaded.threaded(report())
            for item in range(5):
                bridge.send(item)
            print("main done", flush=True)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROTOTYPE_DIR,
            capture_output=True,
            text=True,
            timeout=GUARD_SECONDS,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("target closed after [0, 1, 2, 3, 4]", result.stdout)

    def test_close_delivers_the_backlog_first(self) -> None:
        release = threading.Event()
        holding = threading.Event()
        got: list[int] = []
        with recorded_queues(on_full=release):
            bridge = threaded.threaded(held(got, release, holding), maxsize=3)
            bridge.send(0)
            self.assertTrue(holding.wait(GUARD_SECONDS))
            for item in (1, 2, 3):  # fills the queue without waiting
                bridge.send(item)
            bridge.close()  # its END finds the queue full, releasing item 0
        self.assertEqual(got, [0, 1, 2, 3])

    def test_unreferenced_bridge_is_joined_by_garbage_collection(self) -> None:
        bridge = threaded.threaded(recorder([]), name="collect-me")
        bridge.send(1)
        del bridge
        gc.collect()
        self.assertNotIn("collect-me", worker_names())

    def test_close_is_idempotent_and_send_after_close_stops(self) -> None:
        bridge = threaded.threaded(recorder([]))
        bridge.close()
        bridge.close()
        with self.assertRaises(StopIteration):
            bridge.send(1)

    def test_target_is_closed_on_the_worker_thread(self) -> None:
        closed_on: list[str] = []

        @coroutine
        def notes_close() -> Generator[None, int, None]:
            try:
                while True:
                    yield
            finally:
                closed_on.append(threading.current_thread().name)

        threaded.threaded(notes_close(), name="closer").close()
        self.assertEqual(closed_on, ["closer"])


class AbortTest(ThreadedTestCase):
    def test_exception_in_with_block_discards_backlog(self) -> None:
        release = threading.Event()
        holding = threading.Event()
        got: list[int] = []

        with (
            recorded_queues(on_full=release),
            self.assertRaisesRegex(RuntimeError, "producer failed"),
            threaded.in_thread(held(got, release, holding), maxsize=3) as sink,
        ):
            sink.send(0)
            self.assertTrue(holding.wait(GUARD_SECONDS))
            for item in (1, 2, 3):
                sink.send(item)
            raise RuntimeError("producer failed")
        # The abort's wake-up put found the queue full, releasing item 0;
        # the worker then stopped instead of delivering 1, 2 and 3.
        self.assertEqual(got, [0])

    def test_with_block_exits_normally_and_delivers_everything(self) -> None:
        got: list[int] = []
        with threaded.in_thread(recorder(got), maxsize=2) as sink:
            for item in range(20):
                sink.send(item)
        self.assertEqual(got, list(range(20)))

    def test_ctrl_c_during_a_run_aborts_and_joins(self) -> None:
        # The target interrupts the process once the producer is blocked on
        # a full queue, which is where a real Ctrl+C usually lands.
        blocked = threading.Event()
        got: list[int] = []

        @coroutine
        def interrupts() -> Generator[None, int, None]:
            got.append((yield))
            if not blocked.wait(GUARD_SECONDS):
                raise AssertionError("producer never blocked")
            os.kill(os.getpid(), signal.SIGINT)
            while True:
                got.append((yield))

        with (
            recorded_queues(on_full=blocked),
            self.assertRaises(KeyboardInterrupt),
            threaded.in_thread(interrupts(), maxsize=4) as sink,
        ):
            for item in range(10_000):
                sink.send(item)
        self.assertLess(len(got), 10_000)
        self.assertEqual(got, list(range(len(got))), "order kept")


class ErrorTest(ThreadedTestCase):
    def test_silent_target_death_bug_error_reaches_sender_on_send(
        self,
    ) -> None:
        # The first version let the target's exception kill the worker
        # thread; the sender never heard about it and kept queueing.
        got: list[int] = []
        with recorded_queues() as queues:
            bridge = threaded.threaded(fails_on(0, got), name="fragile")
            bridge.send(0)
            queues[0].join()  # the worker has handled item 0
            with self.assertRaisesRegex(ValueError, "cannot handle 0") as err:
                bridge.send(1)
        self.assertIn("'fragile'", str(err.exception.__notes__))
        with self.assertRaises(StopIteration):
            bridge.send(2)
        bridge.close()  # already reported: does not raise again
        self.assertEqual(got, [])

    def test_error_on_last_item_is_raised_by_close(self) -> None:
        got: list[int] = []
        bridge = threaded.threaded(fails_on(2, got))
        for item in range(3):  # the failure can only be seen after this
            bridge.send(item)
        with self.assertRaisesRegex(ValueError, "cannot handle 2"):
            bridge.close()
        self.assertEqual(got, [0, 1])
        bridge.close()

    def test_error_in_target_cleanup_is_raised_by_close(self) -> None:
        @coroutine
        def bad_cleanup() -> Generator[None, int, None]:
            try:
                while True:
                    yield
            finally:
                raise OSError("flush failed")

        bridge = threaded.threaded(bad_cleanup())
        bridge.send(1)
        with self.assertRaisesRegex(OSError, "flush failed"):
            bridge.close()

    def test_error_during_abort_is_logged_not_lost(self) -> None:
        with recorded_queues() as queues:
            bridge = threaded.threaded(fails_on(0, []), name="doomed")
            bridge.send(0)
            queues[0].join()
            with (
                self.assertLogs("threaded", "ERROR") as logs,
                self.assertRaisesRegex(KeyError, "abort"),
            ):
                bridge.throw(KeyError("abort"))
        self.assertIn("'doomed' also failed", logs.output[0])
        self.assertIn("cannot handle 0", logs.output[0])

    def test_error_reaches_in_thread_caller(self) -> None:
        with (
            self.assertRaisesRegex(ValueError, "cannot handle 3"),
            threaded.in_thread(fails_on(3, []), maxsize=1) as sink,
        ):
            for item in range(10):
                sink.send(item)


class EarlyFinishTest(ThreadedTestCase):
    def test_target_finishing_early_stops_an_infinite_source(self) -> None:
        taken: list[int] = []
        feed(itertools.count(), threaded.threaded(take(3, collect(taken))))
        self.assertEqual(taken, [0, 1, 2])

    def test_send_after_target_finished_raises_stop_iteration(self) -> None:
        taken: list[int] = []
        with recorded_queues() as queues:
            bridge = threaded.threaded(take(1, collect(taken)))
            bridge.send(0)
            queues[0].join()
            with self.assertRaises(StopIteration):
                bridge.send(1)
        self.assertEqual(taken, [0])


class MisuseTest(ThreadedTestCase):
    def test_invalid_arguments_are_rejected_and_target_closed(self) -> None:
        def unprimed() -> Generator[None, int, None]:
            while True:
                yield

        finished = recorder([])
        finished.close()
        for label, target, maxsize in (
            ("maxsize 0", recorder([]), 0),
            ("maxsize -1", recorder([]), -1),
            ("maxsize too big", recorder([]), threaded.MAX_MAXSIZE + 1),
            ("unprimed target", unprimed(), 4),
            ("finished target", finished, 4),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                threaded.threaded(target, maxsize=maxsize)
            self.assertEqual(
                inspect.getgeneratorstate(target), inspect.GEN_CLOSED, label
            )

    def test_thread_start_failure_closes_target(self) -> None:
        target = recorder([])
        cant_start = RuntimeError("can't start new thread")
        with (
            mock.patch.object(
                threading.Thread, "start", side_effect=cant_start
            ),
            self.assertRaisesRegex(RuntimeError, "can't start new thread"),
        ):
            threaded.threaded(target)
        self.assertEqual(inspect.getgeneratorstate(target), inspect.GEN_CLOSED)

    def test_target_using_its_own_bridge_is_stopped_with_an_error(
        self,
    ) -> None:
        holder: list[Sink[int]] = []
        caught: list[BaseException] = []
        sent = threading.Event()

        @coroutine
        def selfish() -> Generator[None, int, None]:
            while True:
                item = yield
                # Wait until the producer is out of send(); otherwise this is
                # "generator already executing", not the case under test.
                if not sent.wait(GUARD_SECONDS):
                    raise AssertionError("producer never finished send()")
                try:
                    holder[0].send(item + 1)
                except RuntimeError as exc:
                    caught.append(exc)

        bridge = threaded.threaded(selfish(), name="selfish")
        holder.append(bridge)
        with self.assertLogs("threaded", "ERROR"):
            bridge.send(1)
            sent.set()
            for thread in threading.enumerate():
                if thread.name == "selfish":
                    thread.join(GUARD_SECONDS)
        self.assertIn("own bridge", str(caught[0]))
        with self.assertRaises(StopIteration):
            bridge.send(2)


class DemoTest(ThreadedTestCase):
    def test_demo_runs_and_exits_zero(self) -> None:
        out = io.StringIO()
        # assertNoLogs also keeps main()'s basicConfig from installing a
        # handler that would outlive this test.
        with contextlib.redirect_stdout(out), self.assertNoLogs(level="INFO"):
            code = threaded.main(["--items", "4", "--item-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertIn("in order: True", out.getvalue())
        self.assertIn("worker threads still running: 0", out.getvalue())

    def test_invalid_arguments_exit_with_usage_error(self) -> None:
        for argv in (
            ["--maxsize", "0"],
            ["--items", "-1"],
            ["--item-seconds", "nan"],
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exit_,
            ):
                threaded.parse_args(argv)
            self.assertEqual(exit_.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
