"""Exit codes, signal handling, logging and flag parsing for the entry points.

Signals follow the repo convention: the first SIGINT or SIGTERM writes a
notice to stderr and unwinds the program through ``Interrupted`` so its
``finally`` blocks shut everything down; a second one exits at once. The exit
code is 128 + the signal number (130 for SIGINT, 143 for SIGTERM).

Handlers are installed explicitly for both signals. A process started from a
non-interactive shell with ``&`` inherits SIGINT as ignored, and Python would
otherwise keep ignoring it.
"""

import argparse
import logging
import math
import os
import signal
from collections.abc import Callable
from types import FrameType

__all__ = [
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_USAGE",
    "Interrupted",
    "bounded_float",
    "bounded_int",
    "configure_logging",
    "run_main",
]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2  # argparse exits with this on a bad flag.

_SIGNALS = (signal.SIGINT, signal.SIGTERM)

LOG = logging.getLogger("bulkhead")


class Interrupted(BaseException):  # Like KeyboardInterrupt: not an Exception.
    """Raised in the main thread when SIGINT or SIGTERM arrives."""

    def __init__(self, signum: int) -> None:
        """Records which signal arrived."""
        super().__init__(signum)
        self.signum = signum


def _on_first_signal(signum: int, _frame: FrameType | None) -> None:
    name = signal.Signals(signum).name
    # os.write: no buffering, so the notice is visible even if the process is
    # killed while cleaning up.
    os.write(2, f"bulkhead: {name}, shutting down (again to force)\n".encode())
    for sig in _SIGNALS:
        signal.signal(sig, _on_second_signal)
    raise Interrupted(signum)


def _on_second_signal(signum: int, _frame: FrameType | None) -> None:
    os._exit(128 + signum)


def run_main(body: Callable[[], int]) -> int:
    """Runs ``body`` with the repo's signal convention, then restores handlers.

    Args:
        body: The program. Its ``finally`` blocks do the cleanup.

    Returns:
        ``body``'s exit code, or 128 + signal number after an interruption.
    """
    previous = {sig: signal.getsignal(sig) for sig in _SIGNALS}
    for sig in _SIGNALS:
        signal.signal(sig, _on_first_signal)
    try:
        return body()
    except Interrupted as exc:
        LOG.warning(
            "interrupted by %s; shut down cleanly",
            signal.Signals(exc.signum).name,
        )
        return 128 + exc.signum
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def configure_logging(verbosity: int) -> None:
    """Logs to stderr: warnings by default, ``-v`` info, ``-vv`` debug."""
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    """Returns an argparse type accepting integers in ``low..high``."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"must be an integer, got {text!r}"
            ) from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"must be in {low}..{high}, got {value}"
            )
        return value

    return parse


def bounded_float(low: float, high: float) -> Callable[[str], float]:
    """Returns an argparse type accepting finite numbers in ``low..high``."""

    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"must be a number, got {text!r}"
            ) from None
        if not math.isfinite(value) or not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"must be in {low:g}..{high:g}, got {text}"
            )
        return value

    return parse
