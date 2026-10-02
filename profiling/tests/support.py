"""Shared test helpers. Not a test module: the name doesn't match test_*."""

import faulthandler
import signal
import sys
import unittest
import warnings
from pathlib import Path

PROTOTYPE_DIR = Path(__file__).resolve().parent.parent
WATCHDOG_S = 60.0


def line_of(path: Path, marker: str) -> int:
    """Returns the 1-based number of the only line in ``path`` with ``marker``.

    Tests locate planted problems by a marker comment rather than a hard-coded
    line number, so editing the file above it doesn't break them.
    """
    hits = [
        number
        for number, text in enumerate(path.read_text().splitlines(), start=1)
        if marker in text
    ]
    if len(hits) != 1:
        raise AssertionError(f"{marker!r} found {len(hits)} times in {path}")
    return hits[0]


class WatchdogTestCase(unittest.TestCase):
    """Fails a hung test instead of blocking forever.

    Each test gets a faulthandler watchdog that dumps every thread's stack
    and exits, warnings become errors, and SIGINT is reset to Python's
    default handler in case the runner inherited it as ignored.
    """

    def setUp(self) -> None:
        """Arms the watchdog and the warning and signal policies."""
        super().setUp()
        faulthandler.dump_traceback_later(WATCHDOG_S, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("error")
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous)


def python_at_least(minor: int) -> bool:
    """Reports whether this is Python 3.``minor`` or newer."""
    return sys.version_info >= (3, minor)
