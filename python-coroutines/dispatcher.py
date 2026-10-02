"""A parent coroutine fanning jobs out to N workers, beside the asyncio way.

The generator version is a *dispatch* pattern, not a concurrency pattern.
``send()`` is a function call: the parent hands a job to a worker and gets
control back only when the worker reaches its next ``yield``, i.e. after the
job is done. Exactly one job is in flight, however many workers exist.
Concurrency needs an event loop that can suspend a job *while it waits* and run
another; the asyncio pool has one. The demo times both on the same workload.

Run ``python3 dispatcher.py --help`` for options.
"""

import argparse
import asyncio
import itertools
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

import cli
from prime import Sink, coroutine, forward

J = TypeVar("J")
R = TypeVar("R")

MAX_WORKERS = 1000
MAX_JOBS = 1_000_000
MAX_JOB_SECONDS = 60.0


@dataclass(frozen=True)
class Outcome(Generic[J, R]):
    """What happened to one job: its value, or the exception it raised."""

    worker_id: int
    job: J
    value: R | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        """Whether the job completed without raising."""
        return self.error is None


# --- Generator coroutines: dispatch, one job at a time ----------------------


@coroutine
def worker(
    worker_id: int,
    func: Callable[[J], R],
    on_outcome: Callable[[Outcome[J, R]], object],
) -> Generator[None, J, None]:
    """Run ``func`` on each job sent in and report how it went.

    Per-job errors are caught here because an exception that escapes a
    generator ends it for good; a worker that let one bad job propagate could
    never serve another.

    Args:
        worker_id: Identifies this worker in outcomes.
        func: The job handler.
        on_outcome: Receives one :class:`Outcome` per job. If it raises, the
            exception propagates and this worker ends.
    """
    while True:
        job = yield
        try:
            value = func(job)
        except Exception as exc:  # reported in the Outcome, not swallowed
            on_outcome(Outcome(worker_id, job, error=exc))
        else:
            on_outcome(Outcome(worker_id, job, value=value))


@coroutine
def round_robin(workers: Sequence[Sink[J]]) -> Generator[None, J, None]:
    """Parent coroutine: give each job to the next worker in turn.

    The parent owns its workers. ``close()`` raises ``GeneratorExit`` at the
    parent's ``yield`` and its ``finally`` closes every worker. The parent only
    closes its children, never itself, so nothing re-enters a running
    generator.

    Args:
        workers: Primed worker coroutines, closed when the parent stops.

    Raises:
        ValueError: ``workers`` is empty.
        RuntimeError: A worker was closed by something other than the parent.
    """
    try:
        if not workers:
            raise ValueError("round_robin needs at least one worker")
        for worker_id, target in itertools.cycle(enumerate(workers)):
            job = yield
            if not forward(target, job):
                raise RuntimeError(f"worker {worker_id} was closed externally")
    finally:
        for target in workers:
            target.close()


def make_pool(
    size: int,
    func: Callable[[J], R],
    on_outcome: Callable[[Outcome[J, R]], object],
) -> Sink[J]:
    """Create ``size`` workers behind a round-robin parent.

    Args:
        size: Number of workers, 1 to ``MAX_WORKERS``.
        func: The job handler.
        on_outcome: Receives one :class:`Outcome` per job.

    Returns:
        The parent coroutine. ``close()`` it when done.

    Raises:
        ValueError: ``size`` is out of range.
    """
    if not 1 <= size <= MAX_WORKERS:
        raise ValueError(f"size must be in 1..{MAX_WORKERS}, got {size}")
    return round_robin([worker(i, func, on_outcome) for i in range(size)])


# --- asyncio: real concurrency for code that awaits -------------------------


async def run_async_pool(
    jobs: Iterable[J],
    func: Callable[[J], Awaitable[R]],
    workers: int,
    queue_size: int = 1,
) -> list[Outcome[J, R]]:
    """Run ``func`` over ``jobs`` on ``workers`` concurrent tasks.

    Jobs overlap only while ``func`` is suspended in an ``await``. A ``func``
    that blocks (``time.sleep``, CPU work, blocking I/O) holds the event loop's
    only thread, and the pool is back to one job at a time. The bounded queue
    is the backpressure: adding a job waits while ``queue_size`` jobs are
    already queued.

    Args:
        jobs: The jobs to run.
        func: Async job handler.
        workers: Number of worker tasks, 1 to ``MAX_WORKERS``.
        queue_size: Maximum number of queued jobs, at least 1.

    Returns:
        One :class:`Outcome` per job, in completion order.

    Raises:
        ValueError: ``workers`` or ``queue_size`` is out of range.
    """
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be in 1..{MAX_WORKERS}, got {workers}")
    if queue_size < 1:
        # asyncio.Queue(maxsize=0) is unbounded, which would drop backpressure.
        raise ValueError(f"queue_size must be >= 1, got {queue_size}")

    queue: asyncio.Queue[J] = asyncio.Queue(maxsize=queue_size)
    outcomes: list[Outcome[J, R]] = []

    async def serve(worker_id: int) -> None:
        while True:
            job = await queue.get()
            try:
                value = await func(job)
            except Exception as exc:  # reported in the Outcome, not swallowed
                outcomes.append(Outcome(worker_id, job, error=exc))
            else:
                outcomes.append(Outcome(worker_id, job, value=value))
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as group:
        servers = [group.create_task(serve(i)) for i in range(workers)]
        for job in jobs:
            await queue.put(job)
        await queue.join()
        for server in servers:
            server.cancel()
    return outcomes


# --- Demo -------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse and validate the demo's command line.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        The parsed options. Invalid input exits with status 2.
    """
    parser = argparse.ArgumentParser(
        description="Time a generator dispatcher against an asyncio pool."
    )
    parser.add_argument(
        "--jobs", type=cli.bounded_int(0, MAX_JOBS), default=8, help="default 8"
    )
    parser.add_argument(
        "--workers",
        type=cli.bounded_int(1, MAX_WORKERS),
        default=4,
        help="default 4",
    )
    parser.add_argument(
        "--job-seconds",
        type=cli.bounded_seconds(MAX_JOB_SECONDS),
        default=0.05,
        help="simulated wait per job, default 0.05",
    )
    return parser.parse_args(argv)


def _run_demo(jobs: int, workers: int, job_seconds: float) -> None:
    print(f"{jobs} jobs x {job_seconds:g}s simulated wait, {workers} workers")

    def blocking_job(job: int) -> int:
        time.sleep(job_seconds)
        return job

    async def awaiting_job(job: int) -> int:
        await asyncio.sleep(job_seconds)
        return job

    async def blocking_job_in_async_clothing(job: int) -> int:
        time.sleep(job_seconds)  # never yields to the event loop
        return job

    outcomes: list[Outcome[int, int]] = []
    pool = make_pool(workers, blocking_job, outcomes.append)
    start = time.perf_counter()
    try:
        for job in range(jobs):
            pool.send(job)
    finally:
        pool.close()
    elapsed = time.perf_counter() - start
    per_worker = dict(sorted(Counter(o.worker_id for o in outcomes).items()))
    print(f"  generator round-robin  {elapsed:6.3f}s  jobs/worker {per_worker}")

    start = time.perf_counter()
    asyncio.run(run_async_pool(range(jobs), awaiting_job, workers))
    elapsed = time.perf_counter() - start
    print(f"  asyncio, awaiting job  {elapsed:6.3f}s  waits overlap")

    start = time.perf_counter()
    asyncio.run(
        run_async_pool(range(jobs), blocking_job_in_async_clothing, workers)
    )
    elapsed = time.perf_counter() - start
    print(
        f"  asyncio, blocking job  {elapsed:6.3f}s  time.sleep stalls the loop"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Time the generator dispatcher against asyncio on simulated I/O.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        0 on success, 130 after Ctrl+C, 143 after SIGTERM. Usage errors exit
        with status 2 from argparse.
    """
    args = parse_args(argv)
    return cli.run_interruptible(
        lambda: _run_demo(args.jobs, args.workers, args.job_seconds),
        on_interrupt="interrupted; workers closed",
    )


if __name__ == "__main__":
    raise SystemExit(main())
