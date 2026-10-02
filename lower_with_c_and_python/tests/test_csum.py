"""Tests for the ctypes wrapper. Run `make build` first (`make test` does)."""

import ctypes
import enum
import itertools
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType, TracebackType

import csum

INT64_MIN, INT64_MAX = csum.INT64_MIN, csum.INT64_MAX


class Recorder:
    """Progress callback that records calls and can stop at an index."""

    def __init__(self, stop_at: int | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.stop_at = stop_at

    def __call__(self, index: int, partial: int) -> bool:
        self.calls.append((index, partial))
        return index == self.stop_at


class UnraisableCatcher:
    """Collects what ctypes reports through sys.unraisablehook."""

    def __enter__(self) -> list[BaseException | None]:
        self.seen: list[BaseException | None] = []
        self.previous = sys.unraisablehook
        sys.unraisablehook = lambda u: self.seen.append(u.exc_value)
        return self.seen

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        sys.unraisablehook = self.previous


class SumRangeTest(unittest.TestCase):
    def test_matches_python_sum_of_range(self) -> None:
        cases = [
            (0, 0),  # empty
            (5, 2),  # stop < start is empty, like range()
            (0, 1),
            (0, 2),
            (0, 100),  # the first version's input
            (-3, 3),
            (-10, 0),
            (0, 1_000_000),  # result needs more than 32 bits
            (INT64_MAX - 1, INT64_MAX),
            (INT64_MIN, INT64_MIN + 1),
        ]
        for start, stop in cases:
            with self.subTest(start=start, stop=stop):
                self.assertEqual(
                    csum.sum_range(start, stop), sum(range(start, stop))
                )

    def test_accepts_what_range_accepts(self) -> None:
        class Ten(enum.IntEnum):
            VALUE = 10

        class Index:
            def __index__(self) -> int:
                return 4

        self.assertEqual(csum.sum_range(False, True), 0)
        self.assertEqual(csum.sum_range(0, Ten.VALUE), 45)
        self.assertEqual(csum.sum_range(0, Index()), 6)

    def test_rejects_non_integers_like_range(self) -> None:
        for bad in [1.5, "3", None, 2.0]:
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                csum.sum_range(0, bad)  # type: ignore[arg-type]

    def test_out_of_range_input_raises_instead_of_truncating(self) -> None:
        # The hazard being guarded against:
        self.assertEqual(ctypes.c_int64(2**64 + 5).value, 5)
        for name, start, stop in [
            ("stop", 0, 2**64 + 5),
            ("stop", 0, INT64_MAX + 1),
            ("start", INT64_MIN - 1, 0),
        ]:
            with (
                self.subTest(start=start, stop=stop),
                self.assertRaisesRegex(OverflowError, f"^{name}="),
            ):
                csum.sum_range(start, stop)

    def test_partial_sum_overflow_raises_sum_overflow_error(self) -> None:
        for start, stop in [
            (INT64_MAX - 2, INT64_MAX),
            (INT64_MIN, INT64_MIN + 2),
            (INT64_MAX // 2, INT64_MAX // 2 + 3),
        ]:
            with (
                self.subTest(start=start, stop=stop),
                self.assertRaises(csum.SumOverflowError) as ctx,
            ):
                csum.sum_range(start, stop)
            self.assertIsInstance(ctx.exception, OverflowError)

    def test_overflow_happens_exactly_when_a_partial_sum_leaves_int64(
        self,
    ) -> None:
        # Ranges straddling the limits, checked against exact Python ints.
        for start, stop in itertools.chain(
            ((INT64_MAX - k, INT64_MAX) for k in range(1, 5)),
            ((INT64_MIN, INT64_MIN + k) for k in range(1, 5)),
            ((INT64_MIN // 2 - k, INT64_MIN // 2 + 1) for k in range(3)),
        ):
            partials = list(itertools.accumulate(range(start, stop)))
            overflows = any(not INT64_MIN <= p <= INT64_MAX for p in partials)
            with self.subTest(start=start, stop=stop, overflows=overflows):
                if overflows:
                    with self.assertRaises(csum.SumOverflowError):
                        csum.sum_range(start, stop)
                else:
                    self.assertEqual(
                        csum.sum_range(start, stop), sum(range(start, stop))
                    )


class ProgressTest(unittest.TestCase):
    def test_progress_sees_every_element_with_running_sum(self) -> None:
        recorder = Recorder()
        self.assertEqual(csum.sum_range(0, 5, recorder), 10)
        self.assertEqual(
            recorder.calls, [(0, 0), (1, 1), (2, 3), (3, 6), (4, 10)]
        )

    def test_progress_not_called_for_empty_range(self) -> None:
        recorder = Recorder()
        self.assertEqual(csum.sum_range(3, 3, recorder), 0)
        self.assertEqual(recorder.calls, [])

    def test_truthy_return_stops_early_with_partial_sum(self) -> None:
        recorder = Recorder(stop_at=3)
        with self.assertRaises(csum.SumStoppedError) as ctx:
            csum.sum_range(0, 100, recorder)
        self.assertEqual(ctx.exception.partial, 0 + 1 + 2 + 3)
        self.assertEqual(len(recorder.calls), 4)

    def test_callback_exception_stops_c_and_is_reraised(self) -> None:
        calls = 0

        def failing(index: int, _partial: int) -> None:
            nonlocal calls
            calls += 1
            if index == 3:
                raise ValueError("bad element")

        # Not assertRaises: it strips the traceback this test inspects.
        with UnraisableCatcher() as unraisable:
            try:
                csum.sum_range(0, 100, failing)
            except ValueError as exc:
                frames = traceback.extract_tb(exc.__traceback__)
            else:
                self.fail("ValueError was not re-raised")
        self.assertEqual(calls, 4, "C must stop at the failing element")
        self.assertEqual(unraisable, [], "nothing may be swallowed")
        self.assertEqual(frames[-1].name, "failing", "keeps the traceback")

    def test_base_exceptions_from_callback_are_reraised(self) -> None:
        def exits(_index: int, _partial: int) -> None:
            raise SystemExit(3)

        with self.assertRaises(SystemExit) as ctx:
            csum.sum_range(0, 10, exits)
        self.assertEqual(ctx.exception.code, 3)

    def test_raw_ctypes_swallows_callback_exceptions(self) -> None:
        # The failure mode the wrapper exists to prevent: without it, ctypes
        # reports the exception as unraisable and C runs to completion.
        lib = csum.load_library(csum.library_path())
        calls = 0

        def failing(index: int, _partial: int, _data: int | None) -> int:
            nonlocal calls
            calls += 1
            if index == 3:
                raise ValueError("bad element")
            return 0

        callback = csum.CProgressFn(failing)
        result = ctypes.c_int64()
        with UnraisableCatcher() as unraisable:
            status = lib.sum_range(0, 10, callback, None, ctypes.byref(result))
        self.assertEqual((status, result.value, calls), (0, 45, 10))
        self.assertEqual(len(unraisable), 1)
        self.assertIsInstance(unraisable[0], ValueError)

    def test_reentrant_call_from_callback(self) -> None:
        inner: list[int] = []

        def outer(index: int, _partial: int) -> None:
            inner.append(csum.sum_range(0, index + 1))

        self.assertEqual(csum.sum_range(0, 4, outer), 6)
        self.assertEqual(inner, [0, 1, 3, 6])


class InterruptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIs(
            signal.getsignal(signal.SIGINT),
            signal.default_int_handler,
            "these tests assume Python's default SIGINT handler",
        )

    def test_sigint_handler_is_restored_after_the_call(self) -> None:
        seen: list[object] = []
        csum.sum_range(
            0, 2, lambda _i, _p: seen.append(signal.getsignal(signal.SIGINT))
        )
        self.assertIsNot(seen[0], signal.default_int_handler)
        self.assertIs(
            signal.getsignal(signal.SIGINT), signal.default_int_handler
        )

    def test_sigint_during_callback_stops_c_right_after_it(self) -> None:
        # Runs the installed SIGINT handler from inside a callback. This
        # cannot reproduce the race (see the next test); it checks that once
        # SIGINT is seen, C stops after this callback and KeyboardInterrupt
        # surfaces from sum_range with nothing reported as unraisable.
        calls = 0

        def deliver_sigint_once(index: int, _partial: int) -> None:
            nonlocal calls
            calls += 1
            if index == 5:
                handler = signal.getsignal(signal.SIGINT)
                assert callable(handler)
                handler(signal.SIGINT, None)

        with (
            UnraisableCatcher() as unraisable,
            self.assertRaises(KeyboardInterrupt),
        ):
            csum.sum_range(0, 1_000, deliver_sigint_once)
        self.assertEqual(calls, 6)
        self.assertEqual(unraisable, [])

    def test_async_sigint_during_c_call_is_never_lost(self) -> None:
        # The real race: SIGINT arrives from another thread while C loops and
        # calls back millions of times. With the deferral disabled, all 5
        # attempts lost the signal (ctypes reported the KeyboardInterrupt as
        # unraisable and C kept going until the deadline).
        outcomes = [self._interrupt_long_call() for _ in range(5)]
        self.assertEqual(outcomes, [KeyboardInterrupt] * 5)

    def _interrupt_long_call(self) -> type[BaseException] | None:
        """Sends SIGINT once C is calling back; returns what was raised."""
        started = threading.Event()
        deadline = time.monotonic() + 2

        def callback(_index: int, _partial: int) -> None:
            started.set()
            if time.monotonic() > deadline:
                raise TimeoutError("SIGINT was lost; C kept running")

        def interrupt() -> None:
            if started.wait(timeout=5):
                os.kill(os.getpid(), signal.SIGINT)

        sender = threading.Thread(target=interrupt)
        sender.start()
        try:
            csum.sum_range(0, INT64_MAX, callback)
        except BaseException as exc:
            return type(exc)
        finally:
            sender.join()
        return None

    def test_custom_sigint_handler_is_left_alone(self) -> None:
        received: list[int] = []

        def custom(signum: int, _frame: FrameType | None) -> None:
            received.append(signum)

        previous = signal.signal(signal.SIGINT, custom)
        try:
            seen: list[object] = []
            total = csum.sum_range(
                0,
                3,
                lambda _i, _p: seen.append(signal.getsignal(signal.SIGINT)),
            )
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(total, 3)
        self.assertEqual(seen, [custom] * 3)

    def test_ignored_sigint_does_not_stop_c(self) -> None:
        def send_sigint(index: int, _partial: int) -> None:
            if index == 2:
                os.kill(os.getpid(), signal.SIGINT)

        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self.assertEqual(csum.sum_range(0, 10, send_sigint), 45)
        finally:
            signal.signal(signal.SIGINT, previous)


class ThreadingTest(unittest.TestCase):
    def test_callbacks_work_on_worker_threads(self) -> None:
        # signal.signal() raises off the main thread; the wrapper must cope.
        recorder = Recorder()
        with ThreadPoolExecutor(max_workers=1) as pool:
            total = pool.submit(csum.sum_range, 0, 5, recorder).result()
        self.assertEqual(total, 10)
        self.assertEqual(len(recorder.calls), 5)

    def test_concurrent_calls_keep_their_own_exceptions(self) -> None:
        class TaggedError(Exception):
            def __init__(self, tag: int) -> None:
                super().__init__(tag)
                self.tag = tag

        def fail_with(tag: int) -> Callable[[int, int], None]:
            def callback(index: int, _partial: int) -> None:
                if index == tag:
                    raise TaggedError(tag)

            return callback

        def run(tag: int) -> int | None:
            try:
                csum.sum_range(0, 10_000, fail_with(tag))
            except TaggedError as exc:
                return exc.tag
            return None

        tags = list(range(16))
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(run, tags)), tags)


class LibraryLoadingTest(unittest.TestCase):
    def test_missing_library_says_how_to_build_it(self) -> None:
        with self.assertRaisesRegex(csum.LibraryLoadError, "run `make build`"):
            csum.load_library(Path("/nonexistent/build/libsum.dylib"))

    def test_unloadable_file_raises_library_load_error(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".dylib") as junk:
            junk.write(b"not a shared library")
            junk.flush()
            with self.assertRaises(csum.LibraryLoadError) as ctx:
                csum.load_library(Path(junk.name))
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_library_path_has_platform_suffix(self) -> None:
        suffix = ".dylib" if sys.platform == "darwin" else ".so"
        path = csum.library_path()
        self.assertEqual(path.suffix, suffix)
        self.assertEqual(
            path.parent, Path(csum.__file__).resolve().parent / "build"
        )

    def test_loads_from_any_working_directory(self) -> None:
        # The first version loaded './libsum.so', relative to the cwd.
        before = os.getcwd()
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            try:
                path = csum.library_path()
                lib = csum.load_library(path)
            finally:
                os.chdir(before)
        self.assertTrue(path.is_absolute())
        out = ctypes.c_int64()
        status = lib.sum_range(
            0, 100, csum.CProgressFn(), None, ctypes.byref(out)
        )
        self.assertEqual((status, out.value), (0, 4950))


if __name__ == "__main__":
    unittest.main()
