import functools

import clocks
import workload
from support import WatchdogTestCase

DURATION = 0.1


class ClockScenarioTest(WatchdogTestCase):
    scenarios: dict[str, clocks.ClockDeltas]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.scenarios = clocks.clock_scenarios(DURATION)

    def test_sleep_advances_wall_clock_but_not_cpu(self) -> None:
        sleep = self.scenarios["sleep"]
        self.assertGreaterEqual(sleep.wall, DURATION * 0.9)
        self.assertLess(sleep.process_cpu, DURATION / 2)

    def test_cpu_in_this_thread_advances_both_cpu_clocks(self) -> None:
        here = self.scenarios["cpu in this thread"]
        self.assertGreaterEqual(here.process_cpu, DURATION * 0.9)
        self.assertGreaterEqual(here.thread_cpu, DURATION * 0.9)

    def test_other_threads_count_for_the_process_not_this_thread(self) -> None:
        other = self.scenarios["cpu in another thread"]
        self.assertGreaterEqual(other.process_cpu, DURATION * 0.9)
        self.assertLess(other.thread_cpu, DURATION / 2)

    def test_out_of_range_durations_are_rejected(self) -> None:
        for bad in (-0.1, clocks.MAX_DURATION_S + 1, float("nan")):
            with self.subTest(duration=bad), self.assertRaises(ValueError):
                clocks.clock_scenarios(bad)


class TimePerLoopTest(WatchdogTestCase):
    def test_best_is_never_slower_than_worst(self) -> None:
        result = clocks.time_per_loop(lambda: sum(range(100)), number=1000)
        self.assertLessEqual(result.best, result.worst)
        self.assertEqual((result.number, result.repeat), (1000, 5))

    def test_autorange_picks_a_positive_count(self) -> None:
        result = clocks.time_per_loop(lambda: None, repeat=1)
        self.assertGreaterEqual(result.number, 1)

    def test_set_membership_fix_beats_the_planted_hotspot(self) -> None:
        # The fix the profile points to: same answer, far less time. The
        # real gap is about 60x, so a 5x threshold can't flake.
        ids = workload.make_ids(2000)
        slow = clocks.time_per_loop(
            functools.partial(workload.find_duplicates, ids), number=3
        )
        fast = clocks.time_per_loop(
            functools.partial(workload.find_duplicates_fast, ids), number=3
        )
        self.assertGreater(slow.best, 5 * fast.best)

    def test_invalid_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            clocks.time_per_loop(lambda: None, repeat=0)
        with self.assertRaises(ValueError):
            clocks.time_per_loop(lambda: None, number=0)
