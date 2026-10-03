"""Shared test helpers: a hang watchdog, and a thread that records results."""

import faulthandler
import threading
import unittest
import warnings
from collections.abc import Callable
from pathlib import Path

# Upper bound for any wait on something that should happen. It only limits
# how long a broken test takes to fail; passing tests never reach it.
GUARD_S = 5.0

PROTOTYPE_DIR = Path(__file__).resolve().parent.parent


class GuardedTestCase(unittest.TestCase):
    """Fails a hung test with a traceback instead of blocking forever."""

    def setUp(self) -> None:
        """Arms the watchdog and turns warnings into errors."""
        faulthandler.dump_traceback_later(60, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("error")

    def expect_error(
        self, fn: Callable[[], object], expected: type[BaseException]
    ) -> BaseException:
        """Asserts ``fn`` raises ``expected`` promptly, on a guarded thread.

        Calls that should be refused or abandoned run off the main thread,
        so a regression that lets them block fails this test cleanly instead
        of hanging the suite.
        """
        caller = Caller(fn)
        caller.start()
        caller.join(GUARD_S)
        self.assertFalse(caller.is_alive(), "the call blocked")
        self.assertIsInstance(caller.error, expected)
        assert caller.error is not None
        return caller.error


class Caller(threading.Thread):
    """Runs ``fn`` on its own thread and keeps its result or exception."""

    def __init__(self, fn: Callable[[], object]) -> None:
        """Prepares the thread; call ``start()`` to run it."""
        # Daemon, so a test that fails while a call is hung cannot stop the
        # test process from exiting. Passing tests always join their callers.
        super().__init__(daemon=True)
        self._fn = fn
        self.result: object = None
        self.error: BaseException | None = None

    def run(self) -> None:
        """Runs the call, capturing any exception."""
        try:
            self.result = self._fn()
        except BaseException as exc:  # Recorded for the test to inspect.
            self.error = exc


def start_callers(fn: Callable[[], object], count: int) -> list[Caller]:
    """Starts ``count`` threads that each call ``fn``."""
    callers = [Caller(fn) for _ in range(count)]
    for caller in callers:
        caller.start()
    return callers


def join_all(test: unittest.TestCase, callers: list[Caller]) -> None:
    """Joins every caller, failing ``test`` if one is still running."""
    for caller in callers:
        caller.join(GUARD_S)
        test.assertFalse(caller.is_alive(), "a caller is still blocked")
