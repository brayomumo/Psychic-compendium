import random
import unittest

from pubsub.backoff import Backoff, ceiling


class CeilingTest(unittest.TestCase):
    def test_doubles_until_the_cap(self) -> None:
        bounds = [ceiling(n, base=0.5, cap=4.0) for n in range(6)]
        self.assertEqual(bounds, [0.5, 1.0, 2.0, 4.0, 4.0, 4.0])

    def test_does_not_overflow_after_many_attempts(self) -> None:
        # base * 2**1100 would overflow a float; the cap must win instead.
        self.assertEqual(ceiling(1100, base=0.5, cap=15.0), 15.0)
        self.assertEqual(ceiling(10**9, base=0.5, cap=15.0), 15.0)

    def test_rejects_bad_arguments(self) -> None:
        for attempt, base, cap in (
            (-1, 1.0, 2.0),
            (0, 0.0, 1.0),
            (0, 2.0, 1.0),
        ):
            with (
                self.subTest(attempt=attempt, base=base, cap=cap),
                self.assertRaises(ValueError),
            ):
                ceiling(attempt, base=base, cap=cap)


class BackoffTest(unittest.TestCase):
    def test_delays_stay_within_the_jitter_window(self) -> None:
        backoff = Backoff(0.5, 4.0, random.Random(1))
        for attempt in range(50):
            delay = backoff.next_delay()
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, ceiling(attempt, base=0.5, cap=4.0))

    def test_is_repeatable_with_a_seed(self) -> None:
        first = Backoff(0.5, 4.0, random.Random(7))
        second = Backoff(0.5, 4.0, random.Random(7))
        self.assertEqual(
            [first.next_delay() for _ in range(10)],
            [second.next_delay() for _ in range(10)],
        )

    def test_jitter_spreads_clients_apart(self) -> None:
        # Many clients failing at once must not retry in lockstep.
        delays = {
            Backoff(1.0, 1.0, random.Random(s)).next_delay() for s in range(50)
        }
        self.assertEqual(len(delays), 50)

    def test_reset_starts_over(self) -> None:
        backoff = Backoff(0.5, 4.0, random.Random(3))
        for _ in range(5):
            backoff.next_delay()
        self.assertEqual(backoff.attempt, 5)
        backoff.reset()
        self.assertEqual(backoff.attempt, 0)
        self.assertLessEqual(backoff.next_delay(), 0.5)

    def test_rejects_cap_below_base(self) -> None:
        with self.assertRaises(ValueError):
            Backoff(2.0, 1.0)


if __name__ == "__main__":
    unittest.main()
