import contextlib
import io
import os
import signal
import subprocess
import sys
from collections.abc import Callable

import bench
import main
from service import ServiceConfig, Variant
from support import GUARD_S, PROTOTYPE_DIR, GuardedTestCase


def run_quietly(entry: Callable[[], int]) -> tuple[int, str]:
    """Runs an entry point with stdout and stderr captured."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = entry()
    return code, out.getvalue()


class DemoTest(GuardedTestCase):
    def test_scenario_numbers_match_the_isolation_model(self) -> None:
        config = ServiceConfig(workers=4, limit=2, timeout=0.1)
        expected = {
            # A answered, B rejected, B timed out, request / pool stuck.
            Variant.SHARED: (0, 0, 0, 4, 0),
            Variant.SEMAPHORE: (4, 6, 0, 2, 0),
            Variant.THREAD_POOL: (4, 6, 2, 0, 2),
        }
        for variant, numbers in expected.items():
            with self.subTest(variant=variant):
                row = main.run_scenario(
                    variant, config, b_requests=8, a_requests=4, window=0.5
                )
                self.assertEqual(
                    (
                        row.a_answered,
                        row.b_rejected,
                        row.b_timed_out,
                        row.request_workers_stuck,
                        row.pool_workers_stuck,
                    ),
                    numbers,
                )
                self.assertEqual(row.threads_leaked, 0)

    def test_demo_prints_every_variant_and_exits_zero(self) -> None:
        code, out = run_quietly(lambda: main.main([]))

        self.assertEqual(code, 0)
        for variant in Variant:
            self.assertIn(f"\n{variant}", out)
        self.assertIn("joined all of its threads", out)

    def test_invalid_flags_exit_2(self) -> None:
        for argv in (["--workers", "0"], ["--limit", "x"], ["--bogus"]):
            with (
                self.subTest(argv=argv),
                self.assertRaises(SystemExit) as raised,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                main.main(argv)
            self.assertEqual(raised.exception.code, 2)


class BenchTest(GuardedTestCase):
    def test_quick_run_reports_every_variant(self) -> None:
        code, out = run_quietly(lambda: bench.main(["--quick"]))

        self.assertEqual(code, 0)
        self.assertIn("running: 3 variants", out)
        for variant in Variant:
            self.assertIn(f"| {variant} |", out)

    def test_rejects_nan_and_out_of_range_values(self) -> None:
        for argv in (["--duration", "nan"], ["--a-share", "2"]):
            with (
                self.subTest(argv=argv),
                self.assertRaises(SystemExit) as raised,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                bench.main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_percentile_uses_nearest_rank(self) -> None:
        values = [float(v) for v in range(1, 101)]
        self.assertEqual(bench.percentile(values, 50), 50.0)
        self.assertEqual(bench.percentile(values, 99), 99.0)
        self.assertEqual(bench.percentile(values, 100), 100.0)
        self.assertEqual(bench.percentile([7.0], 99), 7.0)
        with self.assertRaises(ValueError):
            bench.percentile([], 50)
        with self.assertRaises(ValueError):
            bench.percentile([1.0], 0)


# Starts the real command with SIGINT ignored, the way a non-interactive
# shell starts a `&` job: ignored dispositions survive exec(). This avoids
# preexec_fn, which is unsafe in a process that has threads.
IGNORING_SIGINT = [
    "-c",
    "import os, signal, sys\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
]


class SignalTest(GuardedTestCase):
    """Real processes, launched with Popen, never with a shell ``&``."""

    def start(self, argv: list[str]) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, *argv],
            cwd=PROTOTYPE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # Its own process group, checked below.
        )
        self.addCleanup(self.kill_group, proc)
        return proc

    def kill_group(self, proc: subprocess.Popen[str]) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()

    def assert_group_gone(self, proc: subprocess.Popen[str]) -> None:
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid, 0)

    def start_long_bench(
        self, launcher: list[str] | None = None
    ) -> subprocess.Popen[str]:
        proc = self.start(
            [
                *(launcher or []),
                "bench.py",
                "--duration",
                "600",
                "--repetitions",
                "1",
            ]
        )
        assert proc.stdout is not None
        # The banner is printed once the signal handlers are installed.
        self.assertIn("running:", proc.stdout.readline())
        return proc

    def test_signals_shut_down_cleanly_with_128_plus_signal(self) -> None:
        for sig, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=sig.name):
                proc = self.start_long_bench()

                proc.send_signal(sig)
                _, err = proc.communicate(timeout=GUARD_S)

                self.assertEqual(proc.returncode, code, err)
                self.assertIn(f"bulkhead: {sig.name}, shutting down", err)
                self.assertIn(f"interrupted by {sig.name}; shut down", err)
                self.assertNotIn("Traceback", err)
                self.assert_group_gone(proc)

    def test_sigint_works_even_when_inherited_as_ignored(self) -> None:
        probe = self.start(
            [
                *IGNORING_SIGINT,
                "-c",
                "import signal; "
                "print(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)",
            ]
        )
        out, _ = probe.communicate(timeout=GUARD_S)
        self.assertEqual(out.strip(), "True")  # The precondition holds.

        proc = self.start_long_bench(IGNORING_SIGINT)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=GUARD_S)

        self.assertEqual(proc.returncode, 130, err)

    def test_second_signal_forces_exit_when_cleanup_hangs(self) -> None:
        script = (
            "import threading\n"
            "from _cli import run_main\n"
            "def body():\n"
            "    try:\n"
            "        print('ready', flush=True)\n"
            "        threading.Event().wait()\n"
            "    finally:\n"
            "        print('cleaning', flush=True)\n"
            "        threading.Event().wait()\n"
            "raise SystemExit(run_main(body))\n"
        )
        proc = self.start(["-c", script])
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline().strip(), "ready")

        proc.send_signal(signal.SIGINT)
        # Wait for the first signal's effect: two signals sent back to back
        # can merge into one.
        self.assertEqual(proc.stdout.readline().strip(), "cleaning")
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=GUARD_S)

        self.assertEqual(proc.returncode, 130, err)
        # The second signal exits at once: one notice, and no claim of a
        # clean shutdown, because the cleanup never finished.
        self.assertEqual(err.count("shutting down (again to force)"), 1)
        self.assertNotIn("shut down cleanly", err)
