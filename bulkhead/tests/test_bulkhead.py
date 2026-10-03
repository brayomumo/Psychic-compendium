import math
import threading

from bulkhead import (
    MAX_LIMIT,
    MAX_QUEUE,
    BulkheadFullError,
    CallTimeoutError,
    SemaphoreBulkhead,
    Stats,
    ThreadPoolBulkhead,
    Unisolated,
)
from service import Dependency, DependencyError
from support import GUARD_S, GuardedTestCase, join_all, start_callers


class UnisolatedTest(GuardedTestCase):
    def test_calls_directly_on_the_callers_thread(self) -> None:
        caller_thread = threading.get_ident()
        seen: list[int] = []
        isolation = Unisolated("A")

        result = isolation.call(lambda: seen.append(threading.get_ident()))

        self.assertIsNone(result)
        self.assertEqual(seen, [caller_thread])
        self.assertEqual(isolation.stats(), Stats(1, 0, 0, 0, 1))

    def test_never_rejects_so_a_hang_holds_every_caller(self) -> None:
        dep = Dependency("B")
        dep.hang()
        self.addCleanup(dep.shutdown)
        isolation = Unisolated("B")

        callers = start_callers(lambda: isolation.call(dep.call), 5)

        self.assertTrue(dep.wait_until(lambda s: s.in_flight == 5, GUARD_S))
        self.assertEqual(isolation.stats().rejected, 0)
        dep.release()
        join_all(self, callers)


class SemaphoreBulkheadTest(GuardedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dep = Dependency("B")
        self.addCleanup(self.dep.shutdown)

    def test_rejects_exactly_at_capacity(self) -> None:
        self.dep.hang()
        bulkhead = SemaphoreBulkhead("B", limit=3)

        callers = start_callers(lambda: bulkhead.call(self.dep.call), 3)
        self.assertTrue(
            self.dep.wait_until(lambda s: s.in_flight == 3, GUARD_S)
        )

        self.expect_error(
            lambda: bulkhead.call(self.dep.call), BulkheadFullError
        )
        self.assertEqual(bulkhead.stats(), Stats(3, 1, 0, 3, 3))

        self.dep.release()
        join_all(self, callers)
        self.assertEqual([c.result for c in callers], ["B: ok"] * 3)
        # Every slot came back when the calls returned.
        self.assertEqual(bulkhead.call(self.dep.call), "B: ok")

    def test_bound_holds_under_contention(self) -> None:
        self.dep.hang()
        bulkhead = SemaphoreBulkhead("B", limit=4)
        start = threading.Barrier(40)

        def contend() -> object:
            start.wait(GUARD_S)
            return bulkhead.call(self.dep.call)

        callers = start_callers(contend, 40)
        self.assertTrue(
            bulkhead.wait_until(lambda s: s.rejected == 36, GUARD_S)
        )

        self.assertEqual(self.dep.stats().in_flight, 4)
        self.dep.release()
        join_all(self, callers)
        self.assertEqual(self.dep.stats().peak_in_flight, 4)
        self.assertEqual(bulkhead.stats().accepted, 4)
        errors = [c.error for c in callers if c.error is not None]
        self.assertEqual(len(errors), 36)
        self.assertTrue(all(isinstance(e, BulkheadFullError) for e in errors))

    def test_bounded_wait_rejects_when_no_slot_frees(self) -> None:
        self.dep.hang()
        bulkhead = SemaphoreBulkhead("B", limit=1, max_wait=0.05)
        callers = start_callers(lambda: bulkhead.call(self.dep.call), 1)
        self.assertTrue(
            self.dep.wait_until(lambda s: s.in_flight == 1, GUARD_S)
        )

        self.expect_error(
            lambda: bulkhead.call(self.dep.call), BulkheadFullError
        )

        self.dep.release()
        join_all(self, callers)

    def test_bounded_wait_admits_a_caller_once_a_slot_frees(self) -> None:
        self.dep.hang()
        bulkhead = SemaphoreBulkhead("B", limit=1, max_wait=GUARD_S)
        first = start_callers(lambda: bulkhead.call(self.dep.call), 1)
        self.assertTrue(
            self.dep.wait_until(lambda s: s.in_flight == 1, GUARD_S)
        )
        second = start_callers(lambda: bulkhead.call(self.dep.call), 1)

        self.dep.release()

        join_all(self, first + second)
        self.assertEqual(second[0].result, "B: ok")
        self.assertEqual(bulkhead.stats().rejected, 0)

    def test_a_failing_call_gives_its_slot_back(self) -> None:
        self.dep.fail()
        bulkhead = SemaphoreBulkhead("B", limit=1)

        for _ in range(3):
            with self.assertRaises(DependencyError):
                bulkhead.call(self.dep.call)

        self.assertEqual(bulkhead.stats(), Stats(3, 0, 0, 0, 1))

    def test_rejects_invalid_configuration(self) -> None:
        for limit, max_wait in [
            (0, 0.0),
            (-1, 0.0),
            (MAX_LIMIT + 1, 0.0),
            (1, -1.0),
            (1, math.nan),
            (1, math.inf),
        ]:
            with (
                self.subTest(limit=limit, max_wait=max_wait),
                self.assertRaises(ValueError),
            ):
                SemaphoreBulkhead("B", limit, max_wait=max_wait)


class ThreadPoolBulkheadTest(GuardedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dep = Dependency("B")
        self.addCleanup(self.dep.shutdown)

    def bulkhead(
        self, workers: int, *, queue_size: int = 0, timeout: float | None
    ) -> ThreadPoolBulkhead:
        bulkhead = ThreadPoolBulkhead(
            "B", workers, queue_size=queue_size, timeout=timeout
        )
        # Cleanups run last-in first-out: the dependency is shut down first,
        # so close() never waits on a hung call.
        self.addCleanup(bulkhead.close)
        self.addCleanup(self.dep.shutdown)
        return bulkhead

    def test_runs_calls_on_its_own_threads(self) -> None:
        bulkhead = self.bulkhead(1, timeout=GUARD_S)

        name = bulkhead.call(lambda: threading.current_thread().name)

        self.assertTrue(name.startswith("bulkhead-B"), name)

    def test_rejects_when_workers_and_queue_are_full(self) -> None:
        self.dep.hang()
        bulkhead = self.bulkhead(2, queue_size=1, timeout=None)

        callers = start_callers(lambda: bulkhead.call(self.dep.call), 3)
        self.assertTrue(bulkhead.wait_until(lambda s: s.accepted == 3, GUARD_S))
        self.assertTrue(
            self.dep.wait_until(lambda s: s.in_flight == 2, GUARD_S)
        )

        self.expect_error(
            lambda: bulkhead.call(self.dep.call), BulkheadFullError
        )

        self.dep.release()
        join_all(self, callers)
        self.assertEqual([c.result for c in callers], ["B: ok"] * 3)
        self.assertEqual(bulkhead.stats().peak_in_flight, 2)
        self.assertEqual(bulkhead.stats().rejected, 1)

    def test_timeout_frees_the_caller_but_the_worker_stays_stuck(
        self,
    ) -> None:
        self.dep.hang()
        bulkhead = self.bulkhead(1, timeout=0.05)

        self.expect_error(
            lambda: bulkhead.call(self.dep.call), CallTimeoutError
        )

        self.assertEqual(bulkhead.stats().timed_out, 1)
        # The abandoned call still holds the only worker...
        self.assertTrue(
            self.dep.wait_until(lambda s: s.in_flight == 1, GUARD_S)
        )
        # ...so the bulkhead now fails fast instead of piling up callers.
        self.expect_error(
            lambda: bulkhead.call(self.dep.call), BulkheadFullError
        )

        self.dep.release()
        self.assertTrue(
            bulkhead.wait_until(lambda s: s.in_flight == 0, GUARD_S)
        )
        self.assertEqual(bulkhead.call(self.dep.call), "B: ok")

    def test_timed_out_queued_call_is_cancelled_and_never_runs(self) -> None:
        self.dep.hang()
        bulkhead = self.bulkhead(1, queue_size=1, timeout=0.05)

        for _ in range(3):  # One runs and hangs; two queue and get cancelled.
            self.expect_error(
                lambda: bulkhead.call(self.dep.call), CallTimeoutError
            )

        self.assertEqual(self.dep.stats().started, 1)
        self.assertEqual(bulkhead.stats().timed_out, 3)

    def test_timeout_raised_by_the_call_itself_is_not_a_bulkhead_timeout(
        self,
    ) -> None:
        bulkhead = self.bulkhead(1, timeout=GUARD_S)

        def socket_timeout() -> None:
            raise TimeoutError("read timed out")

        with self.assertRaises(TimeoutError) as raised:
            bulkhead.call(socket_timeout)

        self.assertNotIsInstance(raised.exception, CallTimeoutError)
        self.assertEqual(bulkhead.stats().timed_out, 0)

    def test_a_failing_call_gives_its_permit_back(self) -> None:
        self.dep.fail()
        bulkhead = self.bulkhead(1, timeout=GUARD_S)

        for _ in range(3):
            with self.assertRaises(DependencyError):
                bulkhead.call(self.dep.call)

        self.assertEqual(bulkhead.stats().rejected, 0)
        self.assertEqual(bulkhead.stats().accepted, 3)

    def test_close_joins_every_pool_thread(self) -> None:
        baseline = threading.active_count()
        bulkhead = ThreadPoolBulkhead("B", 3, timeout=GUARD_S)
        self.addCleanup(bulkhead.close)  # Idempotent; covers a failed assert.
        start = threading.Barrier(3)
        callers = start_callers(
            lambda: bulkhead.call(lambda: start.wait(GUARD_S)), 3
        )
        join_all(self, callers)
        self.assertGreater(threading.active_count(), baseline)

        bulkhead.close()

        self.assertEqual(threading.active_count(), baseline)

    def test_rejects_invalid_configuration(self) -> None:
        for workers, queue_size, timeout in [
            (0, 0, 1.0),
            (MAX_LIMIT + 1, 0, 1.0),
            (1, -1, 1.0),
            (1, MAX_QUEUE + 1, 1.0),
            (1, 0, 0.0),
            (1, 0, -1.0),
            (1, 0, math.nan),
            (1, 0, math.inf),
        ]:
            with (
                self.subTest(workers=workers, queue=queue_size, t=timeout),
                self.assertRaises(ValueError),
            ):
                ThreadPoolBulkhead(
                    "B", workers, queue_size=queue_size, timeout=timeout
                )
