import workload
from support import WatchdogTestCase


class FindDuplicatesTest(WatchdogTestCase):
    def test_slow_and_fast_versions_agree(self) -> None:
        cases = [[], [1], [1, 1], [3, 1, 3, 2, 1, 3], workload.make_ids(501)]
        for ids in cases:
            with self.subTest(ids=ids[:6]):
                self.assertEqual(
                    workload.find_duplicates(ids),
                    workload.find_duplicates_fast(ids),
                )

    def test_make_ids_repeats_the_first_half(self) -> None:
        self.assertEqual(workload.make_ids(6), [0, 1, 2, 0, 1, 2])
        self.assertEqual(workload.make_ids(0), [])
        self.assertEqual(workload.make_ids(1), [0])

    def test_sizes_outside_the_bounds_are_rejected(self) -> None:
        for bad in (-1, workload.MAX_REPORT_SIZE + 1):
            with self.subTest(n=bad), self.assertRaises(ValueError):
                workload.make_ids(bad)
        for func in (
            workload.sum_of_squares_calls,
            workload.sum_of_squares_inline,
            workload.leak,
        ):
            name = func.__name__
            with self.subTest(func=name), self.assertRaises(ValueError):
                func(-1)


class ReportTest(WatchdogTestCase):
    def test_report_counts_every_repeat(self) -> None:
        report = workload.run_report(10)
        self.assertEqual(report, workload.Report(10, 5, "acct-0000000"))

    def test_empty_report_has_no_first_duplicate(self) -> None:
        self.assertEqual(workload.run_report(0), workload.Report(0, 0, None))


class SumOfSquaresTest(WatchdogTestCase):
    def test_calls_and_inline_agree(self) -> None:
        for n in (0, 1, 2, 1000):
            with self.subTest(n=n):
                expected = sum(i * i for i in range(n))
                self.assertEqual(workload.sum_of_squares_calls(n), expected)
                self.assertEqual(workload.sum_of_squares_inline(n), expected)


class LeakTest(WatchdogTestCase):
    def setUp(self) -> None:
        super().setUp()
        workload.forget()
        self.addCleanup(workload.forget)

    def test_remember_returns_the_same_payload_every_time(self) -> None:
        first = workload.remember(7)
        self.assertIs(workload.remember(7), first)
        self.assertEqual(len(first), workload.PAYLOAD_BYTES)

    def test_leak_always_adds_new_keys_until_forgotten(self) -> None:
        self.assertEqual(workload.leak(3), 3)
        self.assertEqual(workload.leak(2), 5)
        workload.forget()
        self.assertEqual(workload.leaked_count(), 0)
