import math
import threading
from concurrent.futures import Future, wait

from service import (
    Dependency,
    Mode,
    Outcome,
    Result,
    Service,
    ServiceConfig,
    Variant,
)
from support import GUARD_S, GuardedTestCase, join_all, start_callers

# Long enough to never end on its own during a test; shutdown() cuts it short.
FOREVER_S = 3600.0


class ServiceTest(GuardedTestCase):
    def make(
        self, variant: Variant, *, workers: int = 4, limit: int = 2
    ) -> tuple[Service, Dependency, Dependency]:
        a, b = Dependency("A"), Dependency("B")
        service = Service(
            variant,
            {"A": a, "B": b},
            ServiceConfig(workers=workers, limit=limit, timeout=0.05),
        )
        # Last in, first out: release the dependencies, then close.
        self.addCleanup(service.close)
        self.addCleanup(b.shutdown)
        self.addCleanup(a.shutdown)
        return service, a, b

    def outcomes(self, futures: list[Future[Result]]) -> list[Outcome]:
        return [f.result(GUARD_S).outcome for f in futures]

    def test_shared_pool_starves_healthy_dependency_when_one_hangs(
        self,
    ) -> None:
        for degrade in ("hang", "slow"):
            with self.subTest(degrade=degrade):
                service, a, b = self.make(Variant.SHARED)
                if degrade == "hang":
                    b.hang()
                else:
                    b.slow(FOREVER_S)

                for _ in range(4):
                    service.submit("B")
                self.assertTrue(
                    b.wait_until(lambda s: s.in_flight == 4, GUARD_S)
                )
                to_a = service.submit("A")

                # Every request worker is blocked in B, so A cannot start.
                # The wait only bounds the test's duration: the outcome is
                # fixed by structure, not by timing.
                done, _ = wait([to_a], timeout=0.2)
                self.assertFalse(done)
                self.assertEqual(a.stats().started, 0)

                b.shutdown()
                self.assertEqual(to_a.result(GUARD_S).outcome, Outcome.OK)

    def test_semaphore_bulkhead_keeps_healthy_dependency_available(
        self,
    ) -> None:
        service, _, b = self.make(Variant.SEMAPHORE)
        b.hang()

        to_b = [service.submit("B") for _ in range(8)]
        isolation = service.isolation("B")
        self.assertTrue(
            isolation.wait_until(lambda s: s.rejected == 6, GUARD_S)
        )
        to_a = [service.submit("A") for _ in range(4)]

        self.assertEqual(self.outcomes(to_a), [Outcome.OK] * 4)
        self.assertEqual(b.stats().in_flight, 2)
        # The two admitted B calls still hold two request workers.
        self.assertEqual(sum(f.running() for f in to_b), 2)

        b.release()
        self.assertEqual(
            sorted(self.outcomes(to_b)),
            sorted([Outcome.OK] * 2 + [Outcome.REJECTED] * 6),
        )
        self.assertEqual(b.stats().peak_in_flight, 2)

    def test_thread_pool_bulkhead_keeps_healthy_dependency_available(
        self,
    ) -> None:
        service, _, b = self.make(Variant.THREAD_POOL)
        b.hang()

        to_b = [service.submit("B") for _ in range(8)]
        to_a = [service.submit("A") for _ in range(4)]

        self.assertEqual(self.outcomes(to_a), [Outcome.OK] * 4)
        self.assertEqual(
            sorted(self.outcomes(to_b)),
            sorted([Outcome.TIMED_OUT] * 2 + [Outcome.REJECTED] * 6),
        )
        # No request worker is left inside B; B's own two workers are.
        self.assertFalse(any(f.running() for f in to_b))
        self.assertEqual(b.stats().in_flight, 2)
        self.assertEqual(b.stats().peak_in_flight, 2)

    def test_fast_failures_do_not_exhaust_the_shared_pool(self) -> None:
        service, _, b = self.make(Variant.SHARED)
        b.fail()

        to_b = [service.submit("B") for _ in range(50)]
        to_a = [service.submit("A") for _ in range(4)]

        self.assertEqual(self.outcomes(to_a), [Outcome.OK] * 4)
        self.assertEqual(self.outcomes(to_b), [Outcome.FAILED] * 50)

    def test_no_threads_leak_once_the_hang_ends_and_the_service_closes(
        self,
    ) -> None:
        for variant in Variant:
            with self.subTest(variant=variant):
                baseline = threading.active_count()
                a, b = Dependency("A"), Dependency("B")
                service = Service(
                    variant,
                    {"A": a, "B": b},
                    ServiceConfig(workers=4, limit=2, timeout=0.05),
                )
                b.hang()
                try:
                    for _ in range(8):
                        service.submit("B")
                    to_a = [service.submit("A") for _ in range(4)]
                    if variant is not Variant.SHARED:
                        self.assertEqual(self.outcomes(to_a), [Outcome.OK] * 4)
                finally:
                    # Always, even if an assertion failed: request workers are
                    # non-daemon threads, and leaving them stuck in B would
                    # hang the interpreter at exit instead of failing a test.
                    b.shutdown()
                    a.shutdown()
                    service.close()

                self.assertEqual(threading.active_count(), baseline)

    def test_results_record_queueing_and_service_time(self) -> None:
        service, _, _ = self.make(Variant.SEMAPHORE)

        result = service.submit("A").result(GUARD_S)

        self.assertEqual(result.dependency, "A")
        self.assertGreaterEqual(result.queued_s, 0)
        self.assertGreaterEqual(result.service_s, 0)
        self.assertAlmostEqual(
            result.total_s, result.queued_s + result.service_s
        )

    def test_unknown_dependency_is_rejected(self) -> None:
        service, _, _ = self.make(Variant.SHARED)

        with self.assertRaisesRegex(ValueError, "unknown dependency 'C'"):
            service.submit("C")

    def test_needs_at_least_one_dependency(self) -> None:
        with self.assertRaises(ValueError):
            Service(Variant.SHARED, {}, ServiceConfig())

    def test_config_reports_every_problem_at_once(self) -> None:
        config = ServiceConfig(
            workers=0, limit=0, queue_size=-1, timeout=0, max_wait=math.nan
        )

        with self.assertRaises(ValueError) as raised:
            config.validate()

        message = str(raised.exception)
        for field in ("workers", "limit", "queue_size", "timeout", "max_wait"):
            self.assertIn(field, message)


class DependencyTest(GuardedTestCase):
    def test_release_wakes_every_hung_call_and_ends_the_hang(self) -> None:
        dep = Dependency("B")
        self.addCleanup(dep.shutdown)
        dep.hang()
        callers = start_callers(dep.call, 3)
        self.assertTrue(dep.wait_until(lambda s: s.in_flight == 3, GUARD_S))

        dep.release()

        join_all(self, callers)
        self.assertIs(dep.mode, Mode.HEALTHY)
        self.assertEqual(dep.call(), "B: ok")  # Later calls do not hang.

    def test_a_new_hang_does_not_re_block_calls_from_an_earlier_one(
        self,
    ) -> None:
        dep = Dependency("B")
        self.addCleanup(dep.shutdown)
        dep.hang()
        first = start_callers(dep.call, 1)
        self.assertTrue(dep.wait_until(lambda s: s.in_flight == 1, GUARD_S))

        dep.release()
        dep.hang()

        join_all(self, first)

    def test_shutdown_cuts_slow_calls_short(self) -> None:
        dep = Dependency("B")
        dep.slow(FOREVER_S)
        callers = start_callers(dep.call, 2)
        self.assertTrue(dep.wait_until(lambda s: s.in_flight == 2, GUARD_S))

        dep.shutdown()

        join_all(self, callers)

    def test_failing_mode_raises_immediately(self) -> None:
        dep = Dependency("B")
        dep.fail()

        with self.assertRaisesRegex(RuntimeError, "B is failing"):
            dep.call()
        self.assertEqual(dep.stats().in_flight, 0)

    def test_rejects_invalid_latency(self) -> None:
        for latency in (-1.0, math.nan, math.inf):
            with self.subTest(latency=latency), self.assertRaises(ValueError):
                Dependency("B", latency)
