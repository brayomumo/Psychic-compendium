"""A stop flag that signal handlers can set safely.

Python runs a signal handler on the main thread, between two bytecodes of
whatever that thread was doing. That could be inside pika's I/O loop, halfway
through building a frame, or while holding a lock. So the handler must not
call into pika, and must not take a lock that the interrupted code might
hold. ``threading.Event.set()`` takes a non-reentrant lock, so it can deadlock
here. The handler only assigns attributes and writes one notice line with
``os.write``, which takes no Python-level lock. The main loop polls
``requested`` at points where stopping is safe.

A second signal means the operator has run out of patience: the process exits
at once. That is safe for data, because the broker requeues every unacked
delivery when the connection drops. The only cost is that a publish in flight
ends with an unknown outcome.
"""

import os
import signal
import time
from types import FrameType

# How often an interruptible sleep re-checks the flag.
_POLL_S = 0.05


class Shutdown:
    """Records a stop request from a signal or from the program itself."""

    def __init__(self, notice_fd: int | None = 2) -> None:
        """Creates a flag with no stop requested.

        Args:
            notice_fd: file descriptor that receives a one-line notice when
                the first signal arrives (stderr by default); None for none.
        """
        self._requested = False
        self._signum: int | None = None
        self._notice_fd = notice_fd

    def install(self) -> None:
        """Routes SIGINT and SIGTERM to this flag. Main thread only."""
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def request(self) -> None:
        """Asks the program to stop, e.g. because its work is done."""
        self._requested = True

    @property
    def requested(self) -> bool:
        """True once a stop has been requested by any means."""
        return self._requested

    @property
    def signum(self) -> int | None:
        """The first signal received, or None if none was."""
        return self._signum

    def sleep(self, seconds: float) -> bool:
        """Sleeps, waking early if a stop is requested.

        Args:
            seconds: how long to sleep.

        Returns:
            True if the full duration elapsed, False if cut short.
        """
        deadline = time.monotonic() + seconds
        while not self._requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, _POLL_S))
        return False

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        if self._requested and self._signum is not None:
            # os._exit skips cleanup on purpose: unwinding from an arbitrary
            # point inside pika could block on the very I/O we are abandoning.
            os._exit(128 + signum)
        self._signum = signum
        self._requested = True
        # A raw write rather than logging, which takes locks. It also tells
        # the operator, or a test, that this signal has been handled: POSIX
        # merges a second identical signal into one still pending.
        if self._notice_fd is not None:
            name = signal.Signals(signum).name
            notice = f"{name}: finishing current work; repeat to quit now\n"
            os.write(self._notice_fd, notice.encode())
