"""Scaffolding shared by every test module."""

from __future__ import annotations

import faulthandler
import signal
import time
import unittest
import warnings
from collections.abc import Callable

# A hung test dumps every thread's stack and exits rather than stalling the
# suite: a hang is exactly the bug class this prototype exists to rule out.
TEST_TIMEOUT_S = 60


class WatchdogTestCase(unittest.TestCase):
    """Fails fast on hangs, and turns every warning into an error.

    Warnings-as-errors catches, among others, Python's DeprecationWarning for
    forking a process that already has threads, which can deadlock children.
    """

    def setUp(self) -> None:
        super().setUp()
        faulthandler.dump_traceback_later(TEST_TIMEOUT_S, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        # A shell starts background jobs with SIGINT ignored, and Python then
        # installs no KeyboardInterrupt handler. Every process we start would
        # inherit that, and signal tests would test nothing. Restore the
        # normal handler; exec resets a handled signal to its default.
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        # unittest resets the filters around each run, so set them per test.
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("error")


def wait_until_stable(
    read: Callable[[], int],
    *,
    interval_s: float = 0.02,
    settle_reads: int = 5,
    timeout_s: float = 10.0,
) -> int:
    """Polls ``read`` until it returns the same value ``settle_reads`` times.

    Used only to observe that something has stopped moving; correctness never
    depends on the timing.
    """
    deadline = time.monotonic() + timeout_s
    last, same = read(), 0
    while same < settle_reads:
        if time.monotonic() > deadline:
            raise AssertionError(f"value never settled; last seen {last}")
        time.sleep(interval_s)
        current = read()
        same = same + 1 if current == last else 0
        last = current
    return last
