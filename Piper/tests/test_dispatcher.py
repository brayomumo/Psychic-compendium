"""BoundedDispatcher: the thread pool that cannot be overfilled."""

from __future__ import annotations

import threading
import time
import unittest

from piper import BoundedDispatcher, resolve_max_in_flight
from support import WatchdogTestCase


class BoundedDispatcherTest(WatchdogTestCase):
    def test_in_flight_work_never_exceeds_the_bound(self) -> None:
        lock = threading.Lock()
        completed = 0

        def slow(item: int) -> None:
            nonlocal completed
            time.sleep(0.002)
            with lock:
                completed += 1

        dispatcher = BoundedDispatcher(slow, workers=2, max_in_flight=3)
        peak = 0
        for submitted in range(1, 61):
            dispatcher.submit(submitted)
            with lock:
                # Completions are counted before a slot is released, so this
                # over-estimates what is in flight; it still must not pass 3.
                peak = max(peak, submitted - completed)
        stats = dispatcher.close()

        self.assertLessEqual(peak, 3)
        self.assertEqual(stats.processed, 60)

    def test_handler_errors_are_counted_and_the_first_few_kept(self) -> None:
        def always_fails(item: int) -> None:
            raise RuntimeError(f"boom {item}")

        dispatcher = BoundedDispatcher(always_fails, workers=2)
        for item in range(25):
            dispatcher.submit(item)
        stats = dispatcher.close()

        self.assertEqual((stats.processed, stats.failures), (0, 25))
        self.assertEqual(len(stats.errors), 10)
        self.assertRegex(
            stats.errors[0], r"^RuntimeError: boom \d+ \(item \d+\)$"
        )

    def test_cancelling_close_drops_queued_items_and_finishes_running(
        self,
    ) -> None:
        release = threading.Event()
        dispatcher: BoundedDispatcher[int] = BoundedDispatcher(
            lambda item: release.wait(), workers=1, max_in_flight=5
        )
        for item in range(5):
            dispatcher.submit(item)  # 0 runs and blocks; 1..4 are queued.

        def submit_late() -> None:
            # Blocks until a slot frees. Only cancellation frees one, and it
            # happens after shutdown has begun, so this submit is refused.
            # That refusal is the cue to let item 0 finish. No timing.
            with self.assertRaises(RuntimeError):
                dispatcher.submit(99)
            release.set()

        late = threading.Thread(target=submit_late)
        late.start()
        stats = dispatcher.close(cancel_pending=True)
        late.join()

        self.assertEqual((stats.processed, stats.cancelled), (1, 4))

    def test_rejects_invalid_limits(self) -> None:
        for workers, max_in_flight in [(0, None), (-1, None), (1, 0), (1, -5)]:
            with (
                self.subTest(workers=workers, max_in_flight=max_in_flight),
                self.assertRaises(ValueError),
            ):
                BoundedDispatcher(
                    print, workers=workers, max_in_flight=max_in_flight
                )

    def test_default_bound_is_twice_the_workers(self) -> None:
        self.assertEqual(resolve_max_in_flight(3, None), 6)
        self.assertEqual(resolve_max_in_flight(3, 1), 1)


if __name__ == "__main__":
    unittest.main()
