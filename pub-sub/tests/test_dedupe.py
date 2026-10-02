import unittest

from pubsub.dedupe import RecentIds


class RecentIdsTest(unittest.TestCase):
    def test_remembers_added_ids(self) -> None:
        seen = RecentIds(3)
        seen.add("a")
        self.assertIn("a", seen)
        self.assertNotIn("b", seen)

    def test_evicts_the_oldest_when_full(self) -> None:
        seen = RecentIds(2)
        for message_id in ("a", "b", "c"):
            seen.add(message_id)
        self.assertNotIn("a", seen)
        self.assertIn("b", seen)
        self.assertIn("c", seen)
        self.assertEqual(len(seen), 2)

    def test_re_adding_refreshes_recency(self) -> None:
        seen = RecentIds(2)
        seen.add("a")
        seen.add("b")
        seen.add("a")
        seen.add("c")
        self.assertIn("a", seen)
        self.assertNotIn("b", seen)

    def test_memory_stays_bounded(self) -> None:
        seen = RecentIds(100)
        for number in range(10_000):
            seen.add(str(number))
        self.assertEqual(len(seen), 100)

    def test_rejects_non_positive_capacity(self) -> None:
        for capacity in (0, -1):
            with self.subTest(capacity), self.assertRaises(ValueError):
                RecentIds(capacity)


if __name__ == "__main__":
    unittest.main()
