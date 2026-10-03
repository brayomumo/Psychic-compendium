"""Process-level behaviour that needs no broker: exit codes and signals."""

import signal
import subprocess
import sys
import time
import unittest

from procs import PROJECT_DIR, Proc, child_env

# Nothing listens on port 1, so every connection attempt is refused at once.
UNREACHABLE = {
    "RABBITMQ_URL": "amqp://guest:guest@127.0.0.1:1/%2F",
    "PUBSUB_RECONNECT_BASE_S": "0.05",
    "PUBSUB_RECONNECT_CAP_S": "0.2",
}
ENTRY_POINTS = ("pubsub.publisher", "pubsub.consumer", "pubsub.demo")


def run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=PROJECT_DIR,
        env=child_env(env or {}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class UsageTest(unittest.TestCase):
    def test_help_lists_the_environment_and_exits_0(self) -> None:
        for module in ENTRY_POINTS:
            with self.subTest(module):
                result = run(module, "--help")
                self.assertEqual(result.returncode, 0)
                self.assertIn("RABBITMQ_URL", result.stdout)

    def test_unknown_argument_exits_2(self) -> None:
        for module in ENTRY_POINTS:
            with self.subTest(module):
                self.assertEqual(run(module, "--count=5").returncode, 2)

    def test_bad_configuration_exits_2_listing_every_problem(self) -> None:
        env = {"PUBSUB_PREFETCH": "0", "RABBITMQ_URL": "http://nope"}
        for module in ENTRY_POINTS:
            with self.subTest(module):
                result = run(module, env=env)
                self.assertEqual(result.returncode, 2)
                self.assertIn("PUBSUB_PREFETCH", result.stderr)
                self.assertIn("RABBITMQ_URL", result.stderr)
                self.assertEqual(result.stdout, "", "stdout is for results")


class UnreachableBrokerTest(unittest.TestCase):
    """With the broker down, both sides keep retrying until told to stop."""

    def check_stops_promptly(
        self, module: str, sig: signal.Signals, expected: int
    ) -> None:
        proc = Proc(module, UNREACHABLE)
        try:
            self.assertTrue(
                proc.wait_until(
                    lambda: sum("cannot reach" in s for s in proc.stderr) >= 2
                ),
                proc.logs(),
            )
            started = time.monotonic()
            proc.signal(sig)
            self.assertEqual(proc.finish(timeout=5), expected, proc.logs())
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(proc.stdout, [])
        finally:
            proc.kill()

    def test_consumer_exits_143_on_sigterm(self) -> None:
        self.check_stops_promptly("pubsub.consumer", signal.SIGTERM, 143)

    def test_consumer_exits_130_on_sigint(self) -> None:
        self.check_stops_promptly("pubsub.consumer", signal.SIGINT, 130)

    def test_publisher_exits_130_on_sigint(self) -> None:
        self.check_stops_promptly("pubsub.publisher", signal.SIGINT, 130)

    def test_publisher_exits_143_on_sigterm(self) -> None:
        self.check_stops_promptly("pubsub.publisher", signal.SIGTERM, 143)

    def test_sigint_works_even_when_inherited_as_ignored(self) -> None:
        # A job started with `&` from a non-interactive shell begins with
        # SIGINT ignored, and Python then installs no KeyboardInterrupt
        # handler. Shutdown.install() sets its handler explicitly, so Ctrl+C
        # or `kill -INT` still triggers the graceful path.
        proc = Proc("pubsub.consumer", UNREACHABLE, sigint_ignored=True)
        try:
            self.assertTrue(proc.wait_for_log("cannot reach"), proc.logs())
            proc.signal(signal.SIGINT)
            self.assertEqual(proc.finish(timeout=5), 130, proc.logs())
            self.assertIn("SIGINT: finishing current work", proc.logs())
        finally:
            proc.kill()

    def test_demo_reports_the_broker_is_down(self) -> None:
        result = run("pubsub.demo", env=UNREACHABLE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("make broker-up", result.stderr)


if __name__ == "__main__":
    unittest.main()
