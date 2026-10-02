"""Tests for the generator dispatcher and the asyncio pool."""

import asyncio
import contextlib
import inspect
import io
import os
import signal
import threading
import unittest
from collections import Counter
from collections.abc import Generator, Iterator

import dispatcher
from dispatcher import Outcome
from prime import coroutine


class InFlight:
    """Counts jobs running at the same moment."""

    def __init__(self) -> None:
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        self.now += 1
        self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        self.now -= 1


def identity(job: int) -> int:
    return job


def run_jobs(size: int, jobs: int) -> list[Outcome[int, int]]:
    outcomes: list[Outcome[int, int]] = []
    pool = dispatcher.make_pool(size, identity, outcomes.append)
    try:
        for job in range(jobs):
            pool.send(job)
    finally:
        pool.close()
    return outcomes


class GeneratorDispatchTest(unittest.TestCase):
    def test_zero_workers_bug_pool_creates_requested_workers(self) -> None:
        # The first version reset the worker count to 0 before its creation
        # loop, so Parent(5) built no workers and crashed on start-up.
        outcomes = run_jobs(size=4, jobs=12)
        self.assertEqual(
            Counter(o.worker_id for o in outcomes), {0: 3, 1: 3, 2: 3, 3: 3}
        )

    def test_lost_workers_bug_every_worker_keeps_receiving_jobs(self) -> None:
        # The first version popped workers off its list and never returned
        # them, so the pool shrank with every job.
        outcomes = run_jobs(size=3, jobs=100)
        self.assertEqual(
            Counter(o.worker_id for o in outcomes), {0: 34, 1: 33, 2: 33}
        )

    def test_no_yield_loop_bug_each_send_completes_one_job(self) -> None:
        # The first version could spin in a loop with no yield. Here every
        # send() returns after exactly one job has finished.
        outcomes: list[Outcome[int, int]] = []
        pool = dispatcher.make_pool(2, identity, outcomes.append)
        for job in range(5):
            pool.send(job)
            self.assertEqual(len(outcomes), job + 1)
        pool.close()

    def test_reentrant_close_bug_close_closes_each_worker_once(self) -> None:
        # The first version's GeneratorExit handler called close() on the
        # generator that was running it: "generator already executing".
        closes: list[int] = []

        @coroutine
        def tracked(worker_id: int) -> Generator[None, int, None]:
            try:
                while True:
                    yield
            finally:
                closes.append(worker_id)

        workers = [tracked(i) for i in range(3)]
        pool = dispatcher.round_robin(workers)
        pool.send(0)
        pool.close()
        pool.close()
        self.assertEqual(sorted(closes), [0, 1, 2])
        self.assertTrue(
            all(
                inspect.getgeneratorstate(w) == inspect.GEN_CLOSED
                for w in [pool, *workers]
            )
        )

    def test_dispatch_runs_exactly_one_job_at_a_time(self) -> None:
        tracker = InFlight()

        def job(n: int) -> int:
            tracker.enter()
            try:
                return n
            finally:
                tracker.leave()

        outcomes: list[Outcome[int, int]] = []
        pool = dispatcher.make_pool(8, job, outcomes.append)
        for n in range(32):
            pool.send(n)
        pool.close()
        self.assertEqual(tracker.peak, 1)
        self.assertEqual(len(outcomes), 32)

    def test_job_error_is_reported_and_worker_keeps_serving(self) -> None:
        def fragile(n: int) -> int:
            if n == 1:
                raise ValueError("bad job")
            return n

        outcomes: list[Outcome[int, int]] = []
        pool = dispatcher.make_pool(1, fragile, outcomes.append)
        for n in range(3):
            pool.send(n)
        pool.close()
        self.assertEqual([o.ok for o in outcomes], [True, False, True])
        self.assertIsInstance(outcomes[1].error, ValueError)
        self.assertEqual(outcomes[2].value, 2)

    def test_outcome_handler_error_propagates_and_closes_workers(self) -> None:
        def explode(_: Outcome[int, int]) -> None:
            raise OSError("disk full")

        workers = [dispatcher.worker(i, identity, explode) for i in range(3)]
        pool = dispatcher.round_robin(workers)
        with self.assertRaisesRegex(OSError, "disk full"):
            pool.send(0)
        for gen in [pool, *workers]:
            self.assertEqual(inspect.getgeneratorstate(gen), inspect.GEN_CLOSED)

    def test_externally_closed_worker_raises_clear_error(self) -> None:
        workers = [
            dispatcher.worker(i, identity, lambda _: None) for i in range(2)
        ]
        pool = dispatcher.round_robin(workers)
        workers[1].close()
        pool.send(0)
        with self.assertRaisesRegex(RuntimeError, "worker 1 was closed"):
            pool.send(1)

    def test_invalid_pool_sizes_are_rejected(self) -> None:
        for size in (0, -1, dispatcher.MAX_WORKERS + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                dispatcher.make_pool(size, identity, lambda _: None)
        with self.assertRaisesRegex(ValueError, "at least one worker"):
            dispatcher.round_robin([])


class AsyncPoolTest(unittest.TestCase):
    def test_awaiting_jobs_run_concurrently(self) -> None:
        # Every job waits at a barrier sized to the worker count, so the
        # pool can only finish if all workers hold a job at the same time.
        workers = 4

        async def scenario() -> list[Outcome[int, int]]:
            barrier = asyncio.Barrier(workers)

            async def job(n: int) -> int:
                await barrier.wait()
                return n

            async with asyncio.timeout(5):
                return await dispatcher.run_async_pool(
                    range(workers * 3), job, workers
                )

        outcomes = asyncio.run(scenario())
        self.assertEqual(
            sorted(o.value or 0 for o in outcomes), list(range(12))
        )

    def test_blocking_jobs_run_one_at_a_time(self) -> None:
        tracker = InFlight()

        async def blocking(n: int) -> int:
            tracker.enter()  # no await before leave(): never yields
            tracker.leave()
            return n

        async def awaiting(n: int) -> int:
            tracker.enter()
            await asyncio.sleep(0)
            tracker.leave()
            return n

        # A queue large enough for every job means all four workers have a
        # job available at once; only blocking can keep them from overlapping.
        asyncio.run(dispatcher.run_async_pool(range(16), blocking, 4, 16))
        self.assertEqual(tracker.peak, 1)
        asyncio.run(dispatcher.run_async_pool(range(16), awaiting, 4, 16))
        self.assertEqual(tracker.peak, 4)

    def test_job_error_is_reported_and_others_complete(self) -> None:
        async def fragile(n: int) -> int:
            await asyncio.sleep(0)
            if n == 2:
                raise ValueError("bad job")
            return n

        outcomes = asyncio.run(dispatcher.run_async_pool(range(5), fragile, 2))
        failed = [o for o in outcomes if not o.ok]
        self.assertEqual(len(outcomes), 5)
        self.assertEqual([o.job for o in failed], [2])

    def test_bounded_queue_pulls_jobs_lazily(self) -> None:
        pulled = 0
        pulled_at_first_job: list[int] = []

        def jobs() -> Iterator[int]:
            nonlocal pulled
            for n in range(100):
                pulled += 1
                yield n

        async def job(n: int) -> int:
            pulled_at_first_job.append(pulled)
            await asyncio.sleep(0)
            return n

        asyncio.run(dispatcher.run_async_pool(jobs(), job, 1, queue_size=1))
        # One job running, one queued, one being put: never the whole list.
        self.assertLessEqual(pulled_at_first_job[0], 3)

    def test_producer_crash_cancels_workers_and_surfaces_error(self) -> None:
        def jobs() -> Iterator[int]:
            yield 1
            raise ValueError("producer crashed")

        async def job(n: int) -> int:
            await asyncio.sleep(0)
            return n

        async def scenario() -> None:
            async with asyncio.timeout(5):
                await dispatcher.run_async_pool(jobs(), job, 3)

        with self.assertRaises(ExceptionGroup) as raised:
            asyncio.run(scenario())
        self.assertEqual(
            [str(e) for e in raised.exception.exceptions], ["producer crashed"]
        )

    def test_invalid_sizes_are_rejected(self) -> None:
        async def job(n: int) -> int:
            return n

        for workers, queue_size in ((0, 1), (1, 0), (-1, 1)):
            with (
                self.subTest(workers=workers, queue_size=queue_size),
                self.assertRaises(ValueError),
            ):
                asyncio.run(
                    dispatcher.run_async_pool(
                        range(1), job, workers, queue_size
                    )
                )


class CommandLineTest(unittest.TestCase):
    def test_invalid_arguments_exit_with_usage_error(self) -> None:
        bad = [
            ["--workers", "0"],
            ["--workers", "abc"],
            ["--workers", str(dispatcher.MAX_WORKERS + 1)],
            ["--jobs", "-1"],
            ["--job-seconds", "-0.1"],
            ["--job-seconds", "nan"],
            ["--job-seconds", "inf"],
            ["--job-seconds", "61"],
        ]
        for argv in bad:
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exit_,
            ):
                dispatcher.parse_args(argv)
            self.assertEqual(exit_.exception.code, 2)

    def test_demo_runs_and_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dispatcher.main(["--jobs", "6", "--job-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertIn("jobs/worker {0: 2, 1: 2, 2: 1, 3: 1}", out.getvalue())

    def test_zero_jobs_is_valid(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatcher.main(["--jobs", "0"]), 0)

    def assert_signal_exit(self, signum: int, expected: int) -> None:
        before = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(0.2, os.kill, (os.getpid(), signum))
        timer.start()
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                code = dispatcher.main(
                    ["--jobs", "500", "--job-seconds", "0.02"]
                )
        finally:
            timer.cancel()
        self.assertEqual(code, expected)
        self.assertIn("interrupted", err.getvalue())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_sigint_mid_run_exits_130(self) -> None:
        self.assert_signal_exit(signal.SIGINT, 130)

    def test_sigterm_mid_run_exits_143(self) -> None:
        self.assert_signal_exit(signal.SIGTERM, 143)


if __name__ == "__main__":
    unittest.main()
