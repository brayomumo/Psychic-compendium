"""run_in_process: the no-IPC control group."""

from __future__ import annotations

import functools
import threading
import unittest

from baseline import run_in_process
from piper import Source, Status
from support import WatchdogTestCase
from workloads import check_record, numbers, records, records_then


class RunInProcessTest(WatchdogTestCase):
    def test_delivers_every_item(self) -> None:
        seen: list[int] = []
        lock = threading.Lock()

        def handle(item: object) -> None:
            with lock:
                seen.append(check_record(item).index)

        report = run_in_process(
            {"p": functools.partial(records, "p", 100)}, handle
        )

        self.assertTrue(report.ok)
        self.assertEqual(sorted(seen), list(range(100)))
        self.assertIsNone(report.producers[0].exitcode)  # No process.

    def test_failing_source_is_reported_and_later_sources_run(self) -> None:
        sources: dict[str, Source[object]] = {
            "bad": functools.partial(records_then, "raise", "bad", 3),
            "good": functools.partial(numbers, 5),
        }
        report = run_in_process(sources, lambda item: None)

        bad, good = report.producers
        self.assertEqual((bad.status, bad.items), (Status.FAILED, 3))
        self.assertIn("RuntimeError", bad.detail)
        self.assertEqual((good.status, good.items), (Status.OK, 5))

    def test_items_travel_by_reference_so_anything_goes(self) -> None:
        lock = threading.Lock()  # Would fail to cross a pipe.
        received: list[object] = []

        report = run_in_process({"p": lambda: [lock]}, received.append)

        self.assertTrue(report.ok)
        self.assertIs(received[0], lock)


if __name__ == "__main__":
    unittest.main()
