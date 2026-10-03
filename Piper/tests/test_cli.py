"""The demo and bench entry points, driven as real processes.

These cover what only a real process tree shows: exit codes, what reaches
stdout versus stderr, and that signals leave no process behind.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import IO

from support import WatchdogTestCase

PIPER_DIR = Path(__file__).resolve().parent.parent
START_METHODS = multiprocessing.get_all_start_methods()


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=PIPER_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def start_long_demo(start_method: str) -> subprocess.Popen[str]:
    """Starts a demo that would run for minutes, in its own process group.

    A new session makes the demo and every process it creates one group, so
    ``os.killpg`` reproduces a terminal's Ctrl+C, and probing the group shows
    whether anything outlived the demo.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "demo.py",
            "-v",
            "--start-method",
            start_method,
            "--items",
            "1000000",
            "--produce-ms",
            "1",
        ],
        cwd=PIPER_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def read_until(stream: IO[str], marker: str) -> str:
    seen = ""
    while marker not in seen:
        line = stream.readline()
        if not line:
            raise AssertionError(f"stream ended before {marker!r}:\n{seen}")
        seen += line
    return seen


def group_is_empty(pgid: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


class DemoTest(WatchdogTestCase):
    def test_default_run_handles_every_record_and_exits_zero(self) -> None:
        result = run_script("demo.py", "--producers", "3", "--items", "50")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "received 150, processed 150, handler errors 0", result.stdout
        )
        self.assertEqual(result.stderr, "")  # Diagnostics only on trouble.

    def test_each_injected_failure_exits_one_and_says_why(self) -> None:
        expected = {
            "raise": "p0        failed",
            "kill": "p0        crashed",
            "unpicklable": "p0        failed",
            "undecodable": "p0        failed",
            "worker-error": "handler errors 4",
        }
        for inject, table_text in expected.items():
            with self.subTest(inject=inject):
                result = run_script(
                    "demo.py",
                    *("--producers", "2", "--items", "20"),
                    *("--inject", inject),
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(table_text, result.stdout)
                self.assertIn("ERROR", result.stderr)

    def test_invalid_arguments_exit_two(self) -> None:
        cases = [
            ["--producers", "-1"],
            ["--workers", "0"],
            ["--batch-size", "0"],
            ["--items", "ten"],
            ["--work-ms", "nan"],
            ["--start-method", "threads"],
        ]
        for args in cases:
            with self.subTest(args=args):
                result = run_script("demo.py", *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error:", result.stderr)

    def test_signals_shut_down_cleanly_and_leave_no_processes(self) -> None:
        cases = [
            # Ctrl+C in a terminal signals the whole foreground group.
            (signal.SIGINT, True, 130, "interrupted by SIGINT"),
            # kill(1) or a supervisor signals just the parent.
            (signal.SIGTERM, False, 143, "terminated by SIGTERM"),
        ]
        for start_method in START_METHODS:
            for signum, whole_group, exit_code, log_line in cases:
                with self.subTest(start_method=start_method, signal=signum):
                    demo = start_long_demo(start_method)
                    try:
                        assert demo.stderr is not None
                        early = read_until(demo.stderr, "started 4 producers")
                        if whole_group:
                            os.killpg(demo.pid, signum)
                        else:
                            os.kill(demo.pid, signum)
                        _, late = demo.communicate(timeout=10)
                        no_survivors = group_is_empty(demo.pid)
                    finally:
                        kill_group(demo.pid)  # Only matters if a test failed.
                        demo.wait()
                    stderr = early + late
                    self.assertEqual(demo.returncode, exit_code, stderr)
                    self.assertIn(log_line, stderr)  # The handler path ran.
                    self.assertNotIn("Traceback", stderr)
                    self.assertTrue(no_survivors, stderr)

    def test_producers_exit_on_their_own_if_the_parent_is_killed(self) -> None:
        # SIGKILL leaves the parent no chance to clean up. Producers hold no
        # read end of their own pipe, so their next send fails and they exit.
        for start_method in START_METHODS:
            with self.subTest(start_method=start_method):
                demo = start_long_demo(start_method)
                try:
                    assert demo.stderr is not None
                    read_until(demo.stderr, "started 4 producers")
                    os.kill(demo.pid, signal.SIGKILL)
                    demo.communicate(timeout=10)
                    no_survivors = group_is_empty(demo.pid)
                finally:
                    kill_group(demo.pid)
                    demo.wait()
                self.assertTrue(no_survivors)


class BenchTest(WatchdogTestCase):
    def test_quick_bench_runs_every_case(self) -> None:
        result = run_script("bench.py", "--quick")

        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line for line in result.stdout.splitlines() if line[:2] == "| "]
        self.assertEqual(len(rows), 8)  # Header plus seven cases.


if __name__ == "__main__":
    unittest.main()
