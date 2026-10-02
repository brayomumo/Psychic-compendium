import functools
import signal
import threading
import time

import sampler
import workload
from support import WatchdogTestCase

REPORT_SIZE = 3000
MIN_SAMPLES = 200


class HotspotTest(WatchdogTestCase):
    sampler: sampler.Sampler

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Sampled once for the class; every test only reads the result.
        cls.sampler = sampler.sample_until(
            functools.partial(workload.run_report, REPORT_SIZE),
            interval=0.001,
            min_samples=MIN_SAMPLES,
            timeout_s=20.0,
        )

    def test_sampler_top_frame_is_the_planted_hotspot(self) -> None:
        # With hundreds of samples and a hotspot that uses well over 80% of
        # the CPU, a self share below 50% would take a vanishingly unlikely
        # run of samples; the margin keeps the test from ever flaking.
        top = self.sampler.top(limit=3)
        self.assertEqual(top[0].function.qualname, "find_duplicates")
        self.assertGreater(top[0].self_share, 0.5)

    def test_entry_point_is_on_nearly_every_stack(self) -> None:
        rows = {r.function.qualname: r for r in self.sampler.top(limit=50)}
        self.assertGreater(rows["run_report"].total_share, 0.9)
        self.assertLess(rows["run_report"].self_share, 0.1)

    def test_collects_at_least_the_requested_samples(self) -> None:
        self.assertGreaterEqual(self.sampler.samples, MIN_SAMPLES)

    def test_table_has_one_line_per_row_plus_header(self) -> None:
        rows = self.sampler.top(limit=3)
        self.assertEqual(len(sampler.format_table(rows).splitlines()), 4)


class CpuTimeTest(WatchdogTestCase):
    def test_sleeping_produces_no_samples(self) -> None:
        # ITIMER_PROF counts CPU time, not wall-clock time: a sleeping
        # process is not running, so the timer doesn't advance.
        with sampler.Sampler(interval=0.001) as idle:
            time.sleep(0.2)
        self.assertLessEqual(idle.samples, 2)

    def test_workload_without_cpu_times_out_instead_of_looping(self) -> None:
        with self.assertRaises(TimeoutError):
            sampler.sample_until(
                functools.partial(time.sleep, 0.05),
                min_samples=1000,
                timeout_s=0.3,
            )


class LifecycleTest(WatchdogTestCase):
    def test_timer_and_handler_are_restored_after_use(self) -> None:
        before = signal.getsignal(signal.SIGPROF)
        with sampler.Sampler():
            workload.run_report(500)
        self.assertEqual(signal.getitimer(signal.ITIMER_PROF), (0.0, 0.0))
        self.assertEqual(signal.getsignal(signal.SIGPROF), before)

    def test_timer_is_stopped_even_when_the_workload_raises(self) -> None:
        with self.assertRaises(ZeroDivisionError), sampler.Sampler():
            _ = 1 / 0
        self.assertEqual(signal.getitimer(signal.ITIMER_PROF), (0.0, 0.0))

    def test_refuses_to_run_outside_the_main_thread(self) -> None:
        errors: list[BaseException] = []

        def start() -> None:
            try:
                with sampler.Sampler():
                    pass
            except RuntimeError as exc:
                errors.append(exc)

        thread = threading.Thread(target=start)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)

    def test_refuses_to_clobber_another_timer(self) -> None:
        outer = sampler.Sampler()
        with outer, self.assertRaises(RuntimeError):
            sampler.Sampler().__enter__()

    def test_invalid_arguments_are_rejected(self) -> None:
        for bad in (0.0, -1.0, 2.0, float("nan")):
            with self.subTest(interval=bad), self.assertRaises(ValueError):
                sampler.Sampler(interval=bad)
        with self.assertRaises(ValueError):
            sampler.Sampler().top(limit=0)
        with self.assertRaises(ValueError):
            sampler.sample_until(lambda: None, min_samples=0)
