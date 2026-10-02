"""Producers and handlers shared by the demo, the benchmark and the tests.

Producers are module-level generator functions so that
``functools.partial(producer, ...)`` pickles by reference, which the spawn
and forkserver start methods need in order to start a child.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, NoReturn

#: Ways a producer can be made to fail after emitting its records.
Failure = Literal["raise", "kill", "unpicklable", "undecodable"]
FAILURES: tuple[Failure, ...] = ("raise", "kill", "unpicklable", "undecodable")


@dataclass(frozen=True, slots=True)
class Record:
    """A stand-in for a rich domain object.

    ``datetime`` and ``Decimal`` defeat ``json`` but pickle fine, so this
    crosses a pipe intact. "Can't JSON-serialize" and "can't pickle" are
    different problems.

    Attributes:
        source: Name of the producer that made it.
        index: Position in that producer's stream.
        created: When it was made, timezone-aware.
        amount: An exact decimal, to show the type survives the trip.
    """

    source: str
    index: int
    created: datetime
    amount: Decimal


def records(source: str, count: int, delay_s: float = 0.0) -> Iterator[Record]:
    """Yields ``count`` records, sleeping ``delay_s`` before each one.

    Args:
        source: Stamped on every record.
        count: How many to yield.
        delay_s: Simulated production time per record.

    Yields:
        Records numbered from 0.
    """
    for index in range(count):
        if delay_s:
            time.sleep(delay_s)
        yield Record(source, index, datetime.now(UTC), Decimal(index) / 100)


def check_record(item: object) -> Record:
    """Handler that proves a record arrived intact, with its real types.

    Args:
        item: The item to check.

    Returns:
        ``item``, typed as a ``Record``.

    Raises:
        TypeError: If ``item`` is not an intact ``Record``.
    """
    if not (
        isinstance(item, Record)
        and isinstance(item.created, datetime)
        and isinstance(item.amount, Decimal)
    ):
        raise TypeError(f"expected an intact Record, got {item!r}")
    return item


def records_then(failure: Failure, source: str, count: int) -> Iterator[object]:
    """Yields ``count`` records, then fails in the requested way.

    Args:
        failure: ``"raise"`` raises; ``"kill"`` SIGKILLs the process;
            ``"unpicklable"`` yields a lock; ``"undecodable"`` yields an
            object that pickles but refuses to unpickle.
        source: Stamped on every record.
        count: Records to yield before failing.

    Yields:
        Records, then possibly one poisoned item.

    Raises:
        RuntimeError: For ``"raise"``.
    """
    yield from records(source, count)
    if failure == "raise":
        raise RuntimeError(f"{source} failed after {count} records")
    if failure == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    elif failure == "unpicklable":
        yield threading.Lock()
    else:
        yield Undecodable()


class Undecodable:
    """Pickles in the producer but raises when the consumer unpickles it."""

    def __reduce__(self) -> tuple[object, tuple[()]]:
        """Points unpickling at a function that always raises."""
        return (_refuse_to_unpickle, ())


def _refuse_to_unpickle() -> NoReturn:
    raise ValueError("this object refuses to be unpickled")


def numbers(count: int) -> Iterator[int]:
    """Yields ``0 .. count-1``: a source whose production costs nothing.

    Args:
        count: How many to yield.

    Yields:
        Consecutive integers.
    """
    yield from range(count)


def cpu_numbers(count: int, rounds: int) -> Iterator[int]:
    """Yields ``count`` values that each cost ``rounds`` arithmetic steps.

    Pure-Python arithmetic holds the GIL, so threads in one process cannot
    run this in parallel; separate processes can.

    Args:
        count: How many to yield.
        rounds: Work per value.

    Yields:
        One pseudo-random integer per input.
    """
    for value in range(count):
        x = value
        for _ in range(rounds):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        yield x


def noop(item: object) -> None:
    """Handler that does nothing, so a benchmark measures only transport.

    Args:
        item: Ignored.
    """
