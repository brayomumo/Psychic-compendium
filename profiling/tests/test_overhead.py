import overhead
from support import WatchdogTestCase

CALLS = 200_000


class OverheadTest(WatchdogTestCase):
    rows: dict[str, overhead.OverheadRow]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        measured = overhead.measure_overhead(CALLS, repetitions=3)
        cls.rows = {row.workload: row for row in measured}

    def test_cprofile_inflates_call_heavy_code_more_than_coarse(self) -> None:
        # Measured gap: about x3.5 against x1.0. Assert the ordering with a
        # margin, never absolute times, so machine speed doesn't matter.
        heavy = self.rows["call-heavy"].cprofile_factor
        coarse = self.rows["coarse"].cprofile_factor
        self.assertGreater(heavy, coarse + 0.5)
        self.assertGreater(heavy, 1.5)

    def test_sampler_costs_less_than_cprofile_on_call_heavy_code(self) -> None:
        row = self.rows["call-heavy"]
        self.assertLess(row.sampler_factor, row.cprofile_factor)

    def test_every_workload_is_measured(self) -> None:
        self.assertEqual(set(self.rows), set(overhead.WORKLOADS))

    def test_table_has_one_line_per_workload_plus_header(self) -> None:
        table = overhead.format_table(list(self.rows.values()))
        self.assertEqual(len(table.splitlines()), len(self.rows) + 2)


class ValidationTest(WatchdogTestCase):
    def test_invalid_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            overhead.measure_overhead(10, repetitions=0)
        with self.assertRaises(ValueError):
            overhead.measure_overhead(10, interval=0.0)
        with self.assertRaises(ValueError):
            overhead.measure_overhead(-1)

    def test_environment_names_python_and_cpus(self) -> None:
        text = overhead.environment()
        self.assertIn("Python", text)
        self.assertIn("CPUs", text)
