import tempfile
import unittest
from pathlib import Path

import blindspots
from support import WatchdogTestCase, python_at_least

CHILD_WORK = 20_000


class Contract:
    """Holder, so unittest doesn't collect the start-method-agnostic base."""

    class ProcessBlindSpot(WatchdogTestCase):
        start_method: blindspots.StartMethod

        def test_parent_profile_lacks_the_childs_hotspot(self) -> None:
            self.assertFalse(
                blindspots.parent_sees_child(self.start_method, CHILD_WORK)
            )

        def test_profiling_inside_children_and_merging_sees_it(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                merged = blindspots.profile_children(
                    self.start_method, 3, CHILD_WORK, Path(tmp)
                )
            profiles = merged.get_stats_profile().func_profiles
            self.assertEqual(profiles["child_hotspot"].ncalls, "3")


class SpawnBlindSpotTest(Contract.ProcessBlindSpot):
    start_method = "spawn"


class ForkBlindSpotTest(Contract.ProcessBlindSpot):
    start_method = "fork"


class ForkserverBlindSpotTest(Contract.ProcessBlindSpot):
    start_method = "forkserver"


class ThreadBlindSpotTest(WatchdogTestCase):
    finding: blindspots.ThreadFinding

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.finding = blindspots.thread_finding(200_000)

    @unittest.skipUnless(python_at_least(12), "sys.monitoring is 3.12+")
    def test_cprofile_records_other_threads_since_3_12(self) -> None:
        self.assertTrue(self.finding.worker_seen)

    @unittest.skipUnless(python_at_least(12), "sys.monitoring is 3.12+")
    def test_only_one_profiler_may_run_since_3_12(self) -> None:
        self.assertIsNotNone(self.finding.second_profiler_error)

    @unittest.skipIf(python_at_least(12), "setprofile-based before 3.12")
    def test_cprofile_sees_only_its_own_thread_before_3_12(self) -> None:
        self.assertFalse(self.finding.worker_seen)
        self.assertIsNone(self.finding.second_profiler_error)

    def test_both_threads_really_did_the_work(self) -> None:
        self.assertGreater(self.finding.worker_cpu, 0.0)
        self.assertGreater(self.finding.main_cpu, 0.0)


class ValidationTest(WatchdogTestCase):
    def test_child_count_out_of_range_is_rejected(self) -> None:
        for bad in (0, blindspots.MAX_CHILDREN + 1):
            with (
                self.subTest(children=bad),
                self.assertRaises(ValueError),
                tempfile.TemporaryDirectory() as tmp,
            ):
                blindspots.profile_children("spawn", bad, 1, Path(tmp))
