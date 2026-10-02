import os
import pstats
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import main
from support import PROTOTYPE_DIR, WatchdogTestCase

QUICK = ["--size", "1000", "--samples", "50", "--leak", "100"]
TIMEOUT_S = 30  # Under the 60 s watchdog, so a hang fails cleanly.


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Runs main.py to completion in a fresh interpreter."""
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=PROTOTYPE_DIR,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )


class CommandTest(WatchdogTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def test_all_runs_every_section_and_exits_zero(self) -> None:
        result = run_cli(*QUICK, "--output", str(self.out / "w.prof"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("running: cprofile"))
        for header in (
            "== cProfile",
            "== sampler",
            "== tracemalloc",
            "== timeit",
            "== clocks",
            "== blind spots: child processes",
            "== blind spots: threads",
        ):
            self.assertIn(header, result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_cprofile_saves_a_loadable_profile(self) -> None:
        path = self.out / "nested" / "run.prof"
        result = run_cli("cprofile", "--size", "500", "--output", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        names = pstats.Stats(str(path)).get_stats_profile().func_profiles
        self.assertIn("find_duplicates", names)

    def test_bench_prints_environment_and_table(self) -> None:
        result = run_cli("bench", "--calls", "20000", "--repetitions", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CPUs", result.stdout)
        self.assertIn("| call-heavy |", result.stdout)

    def test_unwritable_output_exits_1_without_traceback(self) -> None:
        blocker = self.out / "file"
        blocker.write_text("not a directory")
        result = run_cli(
            "cprofile", "--size", "100", "--output", str(blocker / "x.prof")
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class UsageTest(WatchdogTestCase):
    def test_bad_arguments_exit_2(self) -> None:
        cases = [
            ["nonsense"],
            ["--size", "0"],
            ["--size", "20001"],
            ["--size", "abc"],
            ["--top", "51"],
            ["--interval-ms", "nan"],
            ["--interval-ms", "inf"],
            ["--interval-ms", "0"],
            ["--repetitions", "-1"],
            ["cprofile", "extra"],
        ]
        for args in cases:
            with self.subTest(args=args):
                result = run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_help_exits_zero(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--interval-ms", result.stdout)


class SignalTest(WatchdogTestCase):
    """Real signals sent to a real process, launched the way the standard
    requires: Popen with its own session (never a shell `&`, which starts
    the child with SIGINT ignored), and readiness taken from the banner the
    program prints once its handlers are installed."""

    LONG = ("bench", "--calls", "5000000", "--repetitions", "1000")

    def start(self, prefix: tuple[str, ...] = ()) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [*prefix, sys.executable, "main.py", *self.LONG],
            cwd=PROTOTYPE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.kill_group, process)
        assert process.stdout is not None
        banner = process.stdout.readline()
        self.assertTrue(banner.startswith("running:"), banner)
        return process

    @staticmethod
    def kill_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def assert_clean_exit(
        self, process: subprocess.Popen[str], code: int, log: str
    ) -> None:
        _, stderr = process.communicate(timeout=TIMEOUT_S)
        self.assertEqual(process.returncode, code, stderr)
        self.assertIn(log, stderr)
        self.assertNotIn("Traceback", stderr)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)  # Nothing left in the process group.

    def test_ctrl_c_exits_130_after_a_clean_stop(self) -> None:
        process = self.start()
        os.killpg(process.pid, signal.SIGINT)  # Ctrl+C hits the whole group.
        self.assert_clean_exit(
            process, 130, "interrupted by SIGINT; stopped cleanly"
        )

    def test_sigterm_exits_143_after_a_clean_stop(self) -> None:
        process = self.start()
        process.send_signal(signal.SIGTERM)
        self.assert_clean_exit(
            process, 143, "terminated by SIGTERM; stopped cleanly"
        )

    def test_sigint_works_even_when_inherited_as_ignored(self) -> None:
        # `trap '' INT` makes the shell ignore SIGINT, and exec passes that
        # disposition on: Python starts with SIG_IGN, exactly as under `&`.
        process = self.start(("sh", "-c", 'trap "" INT; exec "$0" "$@"'))
        os.killpg(process.pid, signal.SIGINT)
        self.assert_clean_exit(
            process, 130, "interrupted by SIGINT; stopped cleanly"
        )


class SecondSignalTest(WatchdogTestCase):
    def setUp(self) -> None:
        super().setUp()
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        main._install_signal_handlers()
        # The handler writes its notice straight to fd 2; keep test output
        # clean.
        quiet = mock.patch.object(os, "write")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_second_signal_exits_immediately(self) -> None:
        # The real os._exit never returns; SystemExit stands in for that.
        with mock.patch.object(os, "_exit", side_effect=SystemExit) as exit_now:
            with self.assertRaises(KeyboardInterrupt):
                main._on_signal(signal.SIGINT, None)
            exit_now.assert_not_called()
            with self.assertRaises(SystemExit):
                main._on_signal(signal.SIGINT, None)
        exit_now.assert_called_once_with(130)

    def test_sigterm_raises_terminated(self) -> None:
        with self.assertRaises(main.Terminated):
            main._on_signal(signal.SIGTERM, None)
