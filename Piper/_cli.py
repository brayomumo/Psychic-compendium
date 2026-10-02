"""Plumbing shared by the ``demo.py`` and ``bench.py`` entry points."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import TypeVar

_Number = TypeVar("_Number", int, float)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2  # argparse exits with this on bad arguments.
EXIT_SIGINT = 128 + signal.SIGINT
EXIT_SIGTERM = 128 + signal.SIGTERM

LOG = logging.getLogger(__name__)


class TerminatedError(BaseException):
    """Raised in the main thread when SIGTERM arrives.

    A ``BaseException``, like ``KeyboardInterrupt``, so that ``except
    Exception`` blocks do not swallow it on its way to the cleanup code.
    """


def _raise_terminated(signum: int, frame: FrameType | None) -> None:
    raise TerminatedError


def run_main(body: Callable[[], int]) -> int:
    """Runs an entry point, turning SIGINT and SIGTERM into exit codes.

    Both signals unwind the stack as exceptions, so every ``finally`` and
    ``with`` block on the way out runs: that is what stops child processes.

    Args:
        body: The entry point proper. Returns an exit code.

    Returns:
        ``body``'s exit code, or 130 after SIGINT, or 143 after SIGTERM.
    """
    signal.signal(signal.SIGTERM, _raise_terminated)
    try:
        return body()
    except KeyboardInterrupt:
        LOG.warning("interrupted by SIGINT; shut down cleanly")
        return EXIT_SIGINT
    except TerminatedError:
        LOG.warning("terminated by SIGTERM; shut down cleanly")
        return EXIT_SIGTERM


def configure_logging(verbosity: int) -> None:
    """Sends diagnostics to stderr; stdout is reserved for results.

    Args:
        verbosity: 0 for warnings, 1 for info, 2 or more for debug.
    """
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(verbosity, 2)]
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def positive_int(text: str) -> int:
    """Parses an integer >= 1 for argparse."""
    value = _parse(int, text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def non_negative_int(text: str) -> int:
    """Parses an integer >= 0 for argparse."""
    value = _parse(int, text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def non_negative_float(text: str) -> float:
    """Parses a finite float >= 0 for argparse."""
    value = _parse(float, text)
    if not 0 <= value < float("inf"):  # Also rejects NaN.
        raise argparse.ArgumentTypeError(f"must be >= 0, got {text}")
    return value


def _parse(kind: type[_Number], text: str) -> _Number:
    try:
        return kind(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a valid {kind.__name__}: {text!r}"
        ) from None
