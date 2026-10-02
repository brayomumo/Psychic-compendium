"""Command-line plumbing shared by the demos: input validation and signals."""

import argparse
import math
import signal
import sys
from collections.abc import Callable
from types import FrameType


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    """Return an argparse ``type`` that accepts integers in ``low..high``.

    Args:
        low: Smallest accepted value.
        high: Largest accepted value.

    Returns:
        A parser that raises ``argparse.ArgumentTypeError`` for anything else,
        which argparse reports as a usage error (exit status 2).
    """

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            message = f"expected an integer, got {text!r}"
            raise argparse.ArgumentTypeError(message) from None
        if not low <= value <= high:
            message = f"must be in {low}..{high}, got {value}"
            raise argparse.ArgumentTypeError(message)
        return value

    return parse


def bounded_seconds(high: float) -> Callable[[str], float]:
    """Return an argparse ``type`` that accepts finite seconds in ``0..high``.

    Args:
        high: Largest accepted value.

    Returns:
        A parser that rejects negatives, ``nan``, ``inf`` and values above
        ``high`` with ``argparse.ArgumentTypeError``.
    """

    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            message = f"expected a number, got {text!r}"
            raise argparse.ArgumentTypeError(message) from None
        if not math.isfinite(value) or not 0 <= value <= high:
            message = f"must be in 0..{high:g} seconds, got {text}"
            raise argparse.ArgumentTypeError(message)
        return value

    return parse


def run_interruptible(run: Callable[[], object], *, on_interrupt: str) -> int:
    """Run ``run`` so that Ctrl+C and SIGTERM unwind it through ``finally``.

    SIGTERM is turned into ``KeyboardInterrupt``, so both signals take the same
    path and every cleanup block runs. The previous SIGTERM handler is
    restored afterwards. Other exceptions propagate unchanged.

    Args:
        run: The work to do.
        on_interrupt: Printed to stderr after an interrupt.

    Returns:
        0 if ``run`` completed, otherwise 128 plus the signal number: 130 for
        SIGINT, 143 for SIGTERM.
    """
    signals: list[int] = []

    def on_sigterm(signum: int, frame: FrameType | None) -> None:
        signals.append(signum)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        run()
    except KeyboardInterrupt:
        print(on_interrupt, file=sys.stderr)
        return 128 + (signals[0] if signals else signal.SIGINT)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0
