"""Finding where memory grows with tracemalloc.

tracemalloc records the Python stack that made each allocation still alive.
Taking a snapshot before and after some work and comparing them by line shows
which lines allocated memory that is *still held*. That is what a leak looks
like, as opposed to memory that was allocated and freed again.
"""

import linecache
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["Growth", "find_growth", "format_table"]

# Allocations made by tracemalloc itself and by the import system are noise.
_NOISE = (
    tracemalloc.Filter(inclusive=False, filename_pattern=tracemalloc.__file__),
    tracemalloc.Filter(
        inclusive=False, filename_pattern="<frozen importlib._bootstrap>"
    ),
    tracemalloc.Filter(inclusive=False, filename_pattern="<unknown>"),
)


@dataclass(frozen=True)
class Growth:
    """Memory still held, attributed to the line that allocated it.

    Attributes:
        filename: Source file of the allocating line.
        lineno: Line number.
        size_bytes: Net bytes allocated by that line and still alive.
        blocks: Net number of memory blocks still alive.
        source: The source line, stripped.
    """

    filename: str
    lineno: int
    size_bytes: int
    blocks: int
    source: str


def find_growth(work: Callable[[], object], *, limit: int = 5) -> list[Growth]:
    """Runs ``work`` and reports which lines' allocations are still alive.

    If tracemalloc was already tracing, it is left running; otherwise it is
    started for the measurement and stopped again.

    Args:
        work: What to measure, taking no arguments.
        limit: How many lines to report, at least 1.

    Returns:
        Lines with net growth, largest first.

    Raises:
        ValueError: ``limit`` is less than 1.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    started_here = not tracemalloc.is_tracing()
    if started_here:
        tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        work()
        after = tracemalloc.take_snapshot()
    finally:
        if started_here:
            tracemalloc.stop()
    # Filter only after both snapshots: compiling the filters' patterns
    # allocates, and doing it in between would show up as growth.
    before, after = before.filter_traces(_NOISE), after.filter_traces(_NOISE)
    growth: list[Growth] = []
    for diff in after.compare_to(before, "lineno"):
        if diff.size_diff <= 0:
            continue
        frame = diff.traceback[0]
        growth.append(
            Growth(
                filename=frame.filename,
                lineno=frame.lineno,
                size_bytes=diff.size_diff,
                blocks=diff.count_diff,
                source=linecache.getline(frame.filename, frame.lineno).strip(),
            )
        )
        if len(growth) == limit:
            break
    return growth


def format_table(rows: list[Growth]) -> str:
    """Formats rows as a fixed-width table.

    Args:
        rows: Rows from :func:`find_growth`.

    Returns:
        The table, one row per line, with a header.
    """
    lines = [f"{'KiB':>9} {'blocks':>7}  line"]
    for row in rows:
        where = f"{row.filename.rsplit('/', 1)[-1]}:{row.lineno}"
        lines.append(
            f"{row.size_bytes / 1024:9.1f} {row.blocks:7d}  {where}  "
            f"{row.source}"
        )
    return "\n".join(lines)
