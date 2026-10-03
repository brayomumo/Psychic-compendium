"""A small program with planted, known performance problems.

A profiler is only trustworthy if you can check it against ground truth. This
workload has three deliberate problems whose locations are known in advance,
so the tests can assert that each tool finds them:

* :func:`find_duplicates` tests membership in a list inside a loop, which is
  quadratic. It dominates CPU time in :func:`run_report`.
* :func:`sum_of_squares_calls` makes one tiny Python call per element, the
  worst case for a deterministic profiler's per-call overhead.
  :func:`sum_of_squares_inline` does the same arithmetic with no calls.
* :func:`remember` caches every payload forever: the planted memory leak.
"""

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "MAX_CALLS",
    "MAX_REPORT_SIZE",
    "PAYLOAD_BYTES",
    "Report",
    "find_duplicates",
    "find_duplicates_fast",
    "forget",
    "leak",
    "leaked_count",
    "make_ids",
    "normalise",
    "remember",
    "run_report",
    "square",
    "sum_of_squares_calls",
    "sum_of_squares_inline",
    "summarise",
]

MAX_REPORT_SIZE = 20_000
"""Largest report size. The quadratic hotspot does about n**2 / 4 steps."""

MAX_CALLS = 10_000_000
"""Largest element count for the sum-of-squares workloads."""

PAYLOAD_BYTES = 1024
"""Size of each payload :func:`remember` keeps alive."""

_REMEMBERED: dict[int, bytes] = {}


def _check(name: str, value: int, maximum: int) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be in 0..{maximum}, got {value}")


@dataclass(frozen=True)
class Report:
    """What :func:`run_report` found.

    Attributes:
        accounts: Number of account names processed.
        duplicates: Number of IDs that had been seen before.
        first_duplicate: Name of the first duplicate account, if any.
    """

    accounts: int
    duplicates: int
    first_duplicate: str | None


def make_ids(n: int) -> list[int]:
    """Returns ``n`` IDs in which the second half repeats the first.

    Args:
        n: Number of IDs, in ``0..MAX_REPORT_SIZE``.

    Returns:
        ``[0, 1, ..., n // 2 - 1, 0, 1, ...]``, deterministic so that every
        run does the same work.

    Raises:
        ValueError: ``n`` is out of range.
    """
    _check("n", n, MAX_REPORT_SIZE)
    distinct = max(1, n // 2)
    return [i % distinct for i in range(n)]


def normalise(ids: Sequence[int]) -> list[str]:
    """Formats IDs as account names. Linear and cheap.

    Args:
        ids: Account IDs.

    Returns:
        One name per ID.
    """
    return [f"acct-{i:07d}" for i in ids]


def find_duplicates(ids: Sequence[int]) -> list[int]:
    """Returns the IDs that were seen earlier, in order. The planted hotspot.

    ``seen`` is deliberately a list, so each membership test scans it: the
    loop is O(n**2). The ``in`` test runs in C without a Python call, so a
    deterministic profiler charges its time to this function's own time.

    Args:
        ids: Account IDs.

    Returns:
        Each repeated ID, once per repeat.
    """
    seen: list[int] = []
    duplicates: list[int] = []
    for value in ids:
        if value in seen:  # HOTSPOT: a linear scan for every element.
            duplicates.append(value)
        else:
            seen.append(value)
    return duplicates


def find_duplicates_fast(ids: Sequence[int]) -> list[int]:
    """Same result as :func:`find_duplicates` in O(n): the fix it points to.

    Args:
        ids: Account IDs.

    Returns:
        Each repeated ID, once per repeat.
    """
    seen: set[int] = set()
    duplicates: list[int] = []
    for value in ids:
        if value in seen:
            duplicates.append(value)
        else:
            seen.add(value)
    return duplicates


def summarise(names: Sequence[str], duplicates: Sequence[int]) -> Report:
    """Builds the report. Constant time.

    Args:
        names: Account names, one per ID.
        duplicates: Repeated IDs, as returned by :func:`find_duplicates`.

    Returns:
        The report.
    """
    first = f"acct-{duplicates[0]:07d}" if duplicates else None
    return Report(len(names), len(duplicates), first)


def run_report(n: int) -> Report:
    """Runs the whole report over ``n`` IDs.

    Args:
        n: Number of IDs, in ``0..MAX_REPORT_SIZE``.

    Returns:
        The report.

    Raises:
        ValueError: ``n`` is out of range.
    """
    ids = make_ids(n)
    names = normalise(ids)
    duplicates = find_duplicates(ids)
    return summarise(names, duplicates)


def square(x: int) -> int:
    """Returns ``x * x``: a function too small to be worth calling.

    Args:
        x: Any integer.

    Returns:
        Its square.
    """
    return x * x


def sum_of_squares_calls(n: int) -> int:
    """Sums ``i * i`` for ``i < n`` with one Python call per element.

    Args:
        n: Element count, in ``0..MAX_CALLS``.

    Returns:
        The sum.

    Raises:
        ValueError: ``n`` is out of range.
    """
    _check("n", n, MAX_CALLS)
    total = 0
    for i in range(n):
        total += square(i)
    return total


def sum_of_squares_inline(n: int) -> int:
    """Same arithmetic as :func:`sum_of_squares_calls`, with no calls.

    Args:
        n: Element count, in ``0..MAX_CALLS``.

    Returns:
        The sum.

    Raises:
        ValueError: ``n`` is out of range.
    """
    _check("n", n, MAX_CALLS)
    total = 0
    for i in range(n):
        total += i * i
    return total


def remember(key: int) -> bytes:
    """Returns the payload for ``key``, caching it forever. The planted leak.

    Args:
        key: Any integer.

    Returns:
        A ``PAYLOAD_BYTES``-byte payload that stays alive until
        :func:`forget` is called.
    """
    payload = _REMEMBERED.get(key)
    if payload is None:
        payload = bytes(PAYLOAD_BYTES)  # LEAK: kept in the cache forever.
        _REMEMBERED[key] = payload
    return payload


def leak(n: int) -> int:
    """Remembers ``n`` keys that were never seen before.

    Args:
        n: Number of new keys, in ``0..MAX_CALLS``.

    Returns:
        How many payloads the cache holds afterwards.

    Raises:
        ValueError: ``n`` is out of range.
    """
    _check("n", n, MAX_CALLS)
    start = len(_REMEMBERED)
    for key in range(start, start + n):
        remember(key)
    return len(_REMEMBERED)


def leaked_count() -> int:
    """Returns how many payloads :func:`remember` is keeping alive."""
    return len(_REMEMBERED)


def forget() -> None:
    """Drops every remembered payload."""
    _REMEMBERED.clear()
