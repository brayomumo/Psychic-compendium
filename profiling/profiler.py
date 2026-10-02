"""Deterministic profiling with cProfile and pstats.

cProfile records every call and return, so its call counts are exact. Its
times are not: every recorded event costs time that is charged to the code
being measured (see ``overhead.py``), and functions that make many small
calls are inflated the most.

Two columns matter most:

* ``tottime``: time spent in the function's own code, excluding calls it made.
  This is where to look for the hotspot.
* ``cumtime``: time from entry to exit, including everything it called. High
  ``cumtime`` with low ``tottime`` means "the time is somewhere below me".
"""

import cProfile
import io
import pstats
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ParamSpec, TypeVar

__all__ = [
    "SORT_KEYS",
    "FunctionStat",
    "callers_report",
    "format_table",
    "profile_call",
    "save",
    "top_functions",
]

P = ParamSpec("P")
T = TypeVar("T")

SORT_KEYS = ("tottime", "cumtime")
"""The columns :func:`top_functions` can rank by."""


@dataclass(frozen=True)
class FunctionStat:
    """One row of a profile.

    Attributes:
        name: Function name as cProfile reports it. Built-ins look like
            ``<method 'append' of 'list' objects>``.
        location: ``file:line`` where the function is defined, or ``~:0``
            for built-ins.
        calls: Call count as pstats formats it: ``"3"``, or ``"5/1"`` when
            recursion means 5 calls but 1 primitive call.
        tottime: Seconds in the function's own code.
        cumtime: Seconds from entry to exit, including callees.
    """

    name: str
    location: str
    calls: str
    tottime: float
    cumtime: float


def profile_call(
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> tuple[T, pstats.Stats]:
    """Calls ``func`` under cProfile.

    Only one cProfile can be active per interpreter on Python 3.12+, so this
    raises ``ValueError`` if another profiler is already running.

    Args:
        func: What to profile.
        *args: Positional arguments for ``func``.
        **kwargs: Keyword arguments for ``func``.

    Returns:
        ``func``'s result and the collected statistics.
    """
    profiler = cProfile.Profile()
    result = profiler.runcall(func, *args, **kwargs)
    return result, pstats.Stats(profiler)


def top_functions(
    stats: pstats.Stats, *, sort: str = "tottime", limit: int = 5
) -> list[FunctionStat]:
    """Ranks the profiled functions.

    Rows are keyed by function name, as ``pstats.Stats.get_stats_profile``
    does, so two functions with the same name in different files would be
    merged. The workloads here use distinct names.

    Args:
        stats: Statistics from :func:`profile_call` or a loaded ``.prof``.
        sort: One of :data:`SORT_KEYS`.
        limit: How many rows to return, at least 1.

    Returns:
        The top rows, highest first.

    Raises:
        ValueError: ``sort`` or ``limit`` is invalid.
    """
    if sort not in SORT_KEYS:
        raise ValueError(f"sort must be one of {SORT_KEYS}, got {sort!r}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    rows = [
        FunctionStat(
            name=name,
            location=f"{fp.file_name}:{fp.line_number}",
            calls=fp.ncalls,
            tottime=fp.tottime,
            cumtime=fp.cumtime,
        )
        for name, fp in stats.get_stats_profile().func_profiles.items()
    ]
    if sort == "tottime":
        rows.sort(key=lambda row: row.tottime, reverse=True)
    else:
        rows.sort(key=lambda row: row.cumtime, reverse=True)
    return rows[:limit]


def callers_report(stats: pstats.Stats, name: str) -> str:
    """Returns pstats' "who called this function" listing for ``name``.

    Args:
        stats: Profile statistics.
        name: Exact function name.

    Returns:
        The text ``pstats`` prints for ``print_callers``.
    """
    buffer = io.StringIO()
    # print_callers writes to the stream a Stats was built with, so print from
    # a copy that owns the buffer. Stats(stats) raises TypeError; add() works.
    copy = pstats.Stats(stream=buffer)
    copy.add(stats)
    # pstats prints Python functions as "file:line(name)".
    copy.print_callers(rf"\({re.escape(name)}\)")
    return buffer.getvalue()


def save(stats: pstats.Stats, path: Path) -> Path:
    """Writes ``stats`` in the binary ``.prof`` format ``pstats`` loads.

    Args:
        stats: Profile statistics.
        path: Destination. Parent directories are created.

    Returns:
        ``path``.

    Raises:
        OSError: The file could not be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stats.dump_stats(path)
    return path


def format_table(rows: Sequence[FunctionStat]) -> str:
    """Formats rows as a fixed-width table.

    Args:
        rows: Rows from :func:`top_functions`.

    Returns:
        The table, one row per line, with a header.
    """
    lines = [f"{'tottime':>9} {'cumtime':>9} {'calls':>9}  function"]
    for row in rows:
        lines.append(
            f"{row.tottime:9.4f} {row.cumtime:9.4f} {row.calls:>9}  "
            f"{row.name} ({Path(row.location).name})"
        )
    return "\n".join(lines)
