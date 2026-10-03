import os
import signal
import subprocess
import sys
import threading
import time
import unittest

from pubsub.shutdown import Shutdown


class ShutdownTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            sig: signal.getsignal(sig)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }

    def tearDown(self) -> None:
        for sig, handler in self._saved.items():
            signal.signal(sig, handler)

    def test_starts_unrequested(self) -> None:
        shutdown = Shutdown()
        self.assertFalse(shutdown.requested)
        self.assertIsNone(shutdown.signum)

    def test_request_without_a_signal(self) -> None:
        shutdown = Shutdown()
        shutdown.request()
        self.assertTrue(shutdown.requested)
        self.assertIsNone(shutdown.signum)

    def test_second_signal_forces_exit(self) -> None:
        # The handler calls os._exit, so run it in a child process.
        code = (
            "import os, signal, time\n"
            "from pubsub.shutdown import Shutdown\n"
            "s = Shutdown(); s.install()\n"
            "os.kill(os.getpid(), signal.SIGINT)\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
            "time.sleep(10)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        self.assertIn("SIGINT: finishing current work", result.stderr)

    def test_signals_set_the_flag_and_remember_which(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig.name):
                shutdown = Shutdown(notice_fd=None)
                shutdown.install()
                os.kill(os.getpid(), sig)
                self.assertTrue(shutdown.requested)
                self.assertEqual(shutdown.signum, sig)

    def test_signal_after_a_programmatic_stop_is_still_graceful(self) -> None:
        shutdown = Shutdown(notice_fd=None)
        shutdown.install()
        shutdown.request()
        os.kill(os.getpid(), signal.SIGTERM)  # Must not force-exit.
        self.assertEqual(shutdown.signum, signal.SIGTERM)

    def test_sleep_runs_its_full_duration(self) -> None:
        self.assertTrue(Shutdown().sleep(0.01))

    def test_sleep_returns_at_once_when_already_requested(self) -> None:
        shutdown = Shutdown()
        shutdown.request()
        started = time.monotonic()
        self.assertFalse(shutdown.sleep(10))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_sleep_wakes_early_on_request(self) -> None:
        shutdown = Shutdown()
        timer = threading.Timer(0.05, shutdown.request)
        timer.start()
        started = time.monotonic()
        try:
            self.assertFalse(shutdown.sleep(10))
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
