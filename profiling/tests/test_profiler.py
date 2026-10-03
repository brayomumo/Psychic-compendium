import inspect
import pstats
import tempfile
from pathlib import Path

import profiler
import workload
from support import WatchdogTestCase

REPORT_SIZE = 3000


class TopFunctionsTest(WatchdogTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.report, self.stats = profiler.profile_call(
            workload.run_report, REPORT_SIZE
        )

    def test_cprofile_ranks_planted_hotspot_first_by_tottime(self) -> None:
        top = profiler.top_functions(self.stats, sort="tottime", limit=3)
        self.assertEqual(top[0].name, "find_duplicates")
        def_line = inspect.getsourcelines(workload.find_duplicates)[1]
        self.assertTrue(top[0].location.endswith(f"workload.py:{def_line}"))

    def test_cumtime_ranks_the_caller_first(self) -> None:
        # cumtime includes callees, so the entry point outranks the hotspot.
        names = [
            row.name
            for row in profiler.top_functions(self.stats, sort="cumtime")
        ]
        self.assertLess(
            names.index("run_report"), names.index("find_duplicates")
        )

    def test_call_counts_are_exact(self) -> None:
        rows = {
            row.name: row
            for row in profiler.top_functions(self.stats, limit=100)
        }
        self.assertEqual(rows["find_duplicates"].calls, "1")
        # One append per element: n // 2 to seen plus n // 2 duplicates.
        self.assertEqual(
            rows["<method 'append' of 'list' objects>"].calls,
            str(REPORT_SIZE),
        )

    def test_profiled_result_is_the_real_result(self) -> None:
        self.assertEqual(self.report, workload.run_report(REPORT_SIZE))

    def test_callers_report_names_the_caller(self) -> None:
        text = profiler.callers_report(self.stats, "find_duplicates")
        self.assertIn("run_report", text)

    def test_invalid_sort_or_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            profiler.top_functions(self.stats, sort="ncalls")
        with self.assertRaises(ValueError):
            profiler.top_functions(self.stats, limit=0)

    def test_table_has_one_line_per_row_plus_header(self) -> None:
        rows = profiler.top_functions(self.stats, limit=4)
        self.assertEqual(len(profiler.format_table(rows).splitlines()), 5)


class SaveTest(WatchdogTestCase):
    def test_saved_profile_loads_back_with_pstats(self) -> None:
        _, stats = profiler.profile_call(workload.run_report, 100)
        with tempfile.TemporaryDirectory() as tmp:
            path = profiler.save(stats, Path(tmp) / "nested" / "run.prof")
            loaded = pstats.Stats(str(path))
            names = set(loaded.get_stats_profile().func_profiles)
        self.assertIn("find_duplicates", names)
