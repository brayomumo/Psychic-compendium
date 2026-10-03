"""The same fan-in with no processes: the control group for ``bench.py``.

This is NOT inter-process communication. Every source runs in the calling
thread, one after another, and items reach the thread pool by reference:
nothing is pickled, so locks, open connections and ORM objects flow through
untouched. Whatever ``piper.run_pipeline`` costs on top of this is the price
of crossing a process boundary.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from typing import TypeVar

from piper import (
    BoundedDispatcher,
    ProducerOutcome,
    RunReport,
    Source,
    Status,
)

T = TypeVar("T")


def run_in_process(
    sources: Mapping[str, Source[T]],
    handler: Callable[[T], object],
    *,
    workers: int = 4,
    max_in_flight: int | None = None,
) -> RunReport:
    """Feeds every source, in turn, through ``handler`` on a thread pool.

    Args:
        sources: Name to source. Sources need not be picklable.
        handler: Called once per item on a worker thread.
        workers: Number of worker threads.
        max_in_flight: Items queued or running at once; ``None`` for twice
            ``workers``.

    Returns:
        A report in the same shape as ``piper.run_pipeline``'s, with no exit
        codes because there are no processes.

    Raises:
        ValueError: If a limit is invalid.
    """
    dispatcher = BoundedDispatcher(
        handler, workers=workers, max_in_flight=max_in_flight
    )
    outcomes: list[ProducerOutcome] = []
    try:
        for name, source in sources.items():
            outcomes.append(_feed(name, source, dispatcher))
        stats = dispatcher.close()
    except BaseException:
        dispatcher.close(cancel_pending=True)
        raise
    return RunReport(
        producers=tuple(outcomes),
        processed=stats.processed,
        worker_failures=stats.failures,
        worker_errors=stats.errors,
    )


def _feed(
    name: str, source: Source[T], dispatcher: BoundedDispatcher[T]
) -> ProducerOutcome:
    items = 0
    try:
        for item in source():
            dispatcher.submit(item)
            items += 1
    except Exception:
        return ProducerOutcome(
            name, Status.FAILED, items, None, traceback.format_exc()
        )
    return ProducerOutcome(name, Status.OK, items, None)
