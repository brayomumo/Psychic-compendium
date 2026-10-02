"""Tests for the demo entry point's CLI contract."""

import contextlib
import io
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import csum
import main

PROTOTYPE_DIR = Path(__file__).resolve().parent.parent


class WalkthroughTest(unittest.TestCase):
    def test_exits_zero_and_shows_each_boundary_behaviour(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main.main([]), 0)
        out = stdout.getvalue()
        for expected in [
            "sum_range(0, 100)                      4950",
            "SumStoppedError(21)",
            "SumOverflowError",
            "OverflowError: stop=18446744073709551616 does not fit",
            "TypeError: 'float' object cannot be interpreted",
            "raw ctypes: C kept going               status=0 result=45 "
            "after 10 callbacks",
            "ValueError('bad element 3') re-raised after 4 callbacks",
        ]:
            with self.subTest(expected=expected):
                self.assertIn(expected, out)

    def test_unknown_flag_exits_2(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main(["--no-such-flag"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_library_exits_1_with_build_hint(self) -> None:
        csum._library.cache_clear()
        missing = Path("/nonexistent/build/libsum.dylib")
        try:
            with (
                mock.patch.object(csum, "library_path", return_value=missing),
                self.assertLogs("c-from-python", "ERROR") as logs,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(main.main([]), 1)
        finally:
            csum._library.cache_clear()
        self.assertIn("run `make build`", logs.output[0])
        self.assertEqual(stdout.getvalue(), "", "fails before any output")


class UntilInterruptedTest(unittest.TestCase):
    def _start(self) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "main.py", "--until-interrupted"],
            cwd=PROTOTYPE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, proc)
        assert proc.stderr is not None
        # The first progress line is logged from inside a C callback, so
        # from here on the signal lands while C is running.
        for line in proc.stderr:
            if "index=0 " in line:
                return proc
        self.fail(f"no progress line; exit code {proc.wait()}")

    @staticmethod
    def _reap(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()  # Also closes the pipes.

    def test_sigint_mid_c_call_exits_130_promptly(self) -> None:
        proc = self._start()
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 130)
        self.assertIn("interrupted", stderr)

    def test_sigterm_terminates_immediately(self) -> None:
        # SIGTERM keeps its default action: the process dies at once, even
        # mid-C (a shell reports 143). There is nothing to clean up.
        proc = self._start()
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
