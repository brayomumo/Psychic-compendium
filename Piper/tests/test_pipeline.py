"""run_pipeline's contract, verified under every start method."""

from __future__ import annotations

import contextlib
import functools
import multiprocessing
import os
import signal
import threading
import time
import unittest
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import fixtures
import piper
from piper import (
    ProducerStartError,
    RunReport,
    Source,
    Status,
    run_pipeline,
)
from support import WatchdogTestCase, wait_until_stable
from workloads import check_record, noop, numbers, records, records_then


class Contract:
    """Holder: unittest collects only module-level TestCase classes."""

    class Pipeline(WatchdogTestCase):
        start_method: ClassVar[str]
        # Under fork the child is a copy, so sources are never pickled.
        pickles_sources: ClassVar[bool] = True

        def run_pipeline(
            self,
            producers: Mapping[str, Source[Any]],
            handler: Callable[[Any], object] = noop,
            **options: Any,
        ) -> RunReport:
            return run_pipeline(
                producers,
                handler,
                start_method=self.start_method,
                **options,
            )

        def assert_no_children_left(self) -> None:
            self.assertEqual(multiprocessing.active_children(), [])

        def test_delivers_every_item_exactly_once_with_types_intact(
            self,
        ) -> None:
            seen: list[tuple[str, int]] = []
            lock = threading.Lock()

            def handle(item: object) -> None:
                record = check_record(item)
                with lock:
                    seen.append((record.source, record.index))

            producers = {
                f"p{i}": functools.partial(records, f"p{i}", 100)
                for i in range(3)
            }
            # 7 does not divide 100, so the final partial batch is covered.
            report = self.run_pipeline(producers, handle, batch_size=7)

            self.assertTrue(report.ok, report)
            expected = [(f"p{i}", n) for i in range(3) for n in range(100)]
            self.assertEqual(sorted(seen), expected)
            self.assertEqual([p.items for p in report.producers], [100] * 3)
            self.assertEqual({p.exitcode for p in report.producers}, {0})

        def test_returns_once_producers_finish_instead_of_hanging(
            self,
        ) -> None:
            # The first version never returned: open write ends meant the
            # consumer never saw EOF, and nothing joined its process.
            report = self.run_pipeline({"p": functools.partial(numbers, 10)})
            self.assertTrue(report.ok)
            self.assert_no_children_left()

        def test_zero_producers(self) -> None:
            report = self.run_pipeline({})
            self.assertEqual(report.producers, ())
            self.assertTrue(report.ok)

        def test_producers_with_zero_items(self) -> None:
            empty = functools.partial(numbers, 0)
            report = self.run_pipeline({"a": empty, "b": empty})
            self.assertTrue(report.ok)
            self.assertEqual(report.received, 0)

        def test_raising_producer_is_reported_and_others_finish(self) -> None:
            producers: dict[str, Source[object]] = {
                "bad": functools.partial(records_then, "raise", "bad", 10),
                "good": functools.partial(records, "good", 50),
            }
            report = self.run_pipeline(producers, batch_size=4)

            bad, good = report.producers
            self.assertEqual(
                (bad.status, bad.items, bad.exitcode), (Status.FAILED, 10, 1)
            )
            self.assertIn("RuntimeError: bad failed after 10", bad.detail)
            self.assertEqual((good.status, good.items), (Status.OK, 50))
            self.assertFalse(report.ok)

        def test_killed_producer_is_reported_as_crashed_not_waited_on(
            self,
        ) -> None:
            producers: dict[str, Source[object]] = {
                "bad": functools.partial(records_then, "kill", "bad", 10),
                "good": functools.partial(records, "good", 50),
            }
            report = self.run_pipeline(producers, batch_size=1)

            bad, good = report.producers
            self.assertEqual(
                (bad.status, bad.items, bad.exitcode),
                (Status.CRASHED, 10, -signal.SIGKILL),
            )
            self.assertIn("killed by SIGKILL", bad.detail)
            self.assertEqual((good.status, good.items), (Status.OK, 50))

        def test_dead_producer_is_detected_while_a_grandchild_holds_its_pipe(
            self,
        ) -> None:
            # EOF cannot reveal this death: the grandchild keeps the write
            # end open, and the process sentinel too. Only waitpid can.
            holders: list[int] = []
            source = functools.partial(fixtures.fork_pipe_holder_then_die, 30)
            started = time.monotonic()
            try:
                report = self.run_pipeline(
                    {"p": source}, holders.append, batch_size=1
                )
            finally:
                for pid in holders:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)

            (producer,) = report.producers
            self.assertEqual(producer.status, Status.CRASHED)
            self.assertIn("exited without end-of-stream", producer.detail)
            self.assertLess(time.monotonic() - started, 10)  # Not 30.

        def test_producers_ignore_sigint_so_the_parent_owns_shutdown(
            self,
        ) -> None:
            # A terminal's Ctrl+C reaches every process in the group. A
            # producer that died of it would race the parent's cleanup.
            source = functools.partial(fixtures.numbers_with_sigint, 100)
            report = self.run_pipeline({"p": source})

            (producer,) = report.producers
            self.assertEqual(
                (producer.status, producer.items), (Status.OK, 100)
            )

        def test_crash_loses_only_the_unsent_batch(self) -> None:
            source = functools.partial(records_then, "kill", "bad", 20)
            report = self.run_pipeline({"bad": source}, batch_size=8)
            self.assertEqual(report.producers[0].items, 16)  # Two batches.

        def test_unpicklable_item_fails_producer_after_earlier_items(
            self,
        ) -> None:
            source = functools.partial(records_then, "unpicklable", "bad", 10)
            report = self.run_pipeline({"bad": source}, batch_size=32)

            (bad,) = report.producers
            self.assertEqual((bad.status, bad.items), (Status.FAILED, 10))
            self.assertIn(
                "UnpicklableItemError: _thread.lock cannot be pickled",
                bad.detail,
            )

        def test_undecodable_item_fails_its_producer_only(self) -> None:
            producers: dict[str, Source[object]] = {
                "bad": functools.partial(records_then, "undecodable", "bad", 5),
                "good": functools.partial(records, "good", 20),
            }
            report = self.run_pipeline(producers, batch_size=1)

            bad, good = report.producers
            self.assertEqual((bad.status, bad.items), (Status.FAILED, 5))
            self.assertIn("could not unpickle", bad.detail)
            self.assertEqual((good.status, good.items), (Status.OK, 20))
            self.assert_no_children_left()

        def test_handler_exceptions_are_counted_not_dropped(self) -> None:
            def reject_odd(item: int) -> None:
                if item % 2:
                    raise ValueError(f"odd {item}")

            source = functools.partial(numbers, 40)
            report = self.run_pipeline({"p": source}, reject_odd)

            self.assertEqual(
                (report.processed, report.worker_failures), (20, 20)
            )
            self.assertEqual(len(report.worker_errors), 10)  # Capped.
            self.assertIn("ValueError: odd", report.worker_errors[0])
            self.assertFalse(report.ok)

        def test_busy_workers_block_producers_through_the_pipe(self) -> None:
            context = multiprocessing.get_context(self.start_method)
            produced = context.Value("q", 0)
            plateau: list[int] = []

            def handle(item: bytes) -> None:
                # Hold the only worker until the producer stops moving.
                if not plateau:
                    plateau.append(wait_until_stable(lambda: produced.value))

            total = 2000
            report = self.run_pipeline(
                {
                    "p": functools.partial(
                        fixtures.counted_blobs, produced, total, 4096
                    )
                },
                handle,
                workers=1,
                max_in_flight=1,
                batch_size=1,
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.processed, total)
            # Bounded by pipe capacity plus in-flight work, not by the stream.
            self.assertLess(plateau[0], total // 4)

        def test_sigint_stops_producers_and_drops_queued_work(self) -> None:
            pids: set[int] = set()
            handled_after_signal = 0
            interrupted = threading.Event()

            def handle(pid: int) -> None:
                nonlocal handled_after_signal
                if interrupted.is_set():
                    handled_after_signal += 1  # Only one worker thread.
                pids.add(pid)
                if len(pids) == 2 and not interrupted.is_set():
                    interrupted.set()
                    os.kill(os.getpid(), signal.SIGINT)
                time.sleep(0.02)  # Simulated work, so a queue builds up.

            endless = {"a": fixtures.endless_pids, "b": fixtures.endless_pids}
            with self.assertRaises(KeyboardInterrupt):
                self.run_pipeline(
                    endless, handle, workers=1, max_in_flight=100, batch_size=1
                )

            self.assertEqual(len(pids), 2)
            for pid in pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)  # Gone, and reaped.
            self.assert_no_children_left()
            # About 100 items were queued; draining them would take 2 s.
            self.assertLessEqual(handled_after_signal, 10)

        def test_unpicklable_source_fails_fast_unless_forking(self) -> None:
            producers: dict[str, Source[int]] = {
                "good": functools.partial(numbers, 1000),
                "lambda": lambda: range(3),
            }
            if not self.pickles_sources:
                self.assertTrue(self.run_pipeline(producers).ok)
                return
            with self.assertRaisesRegex(ProducerStartError, "picklable"):
                self.run_pipeline(producers)
            self.assert_no_children_left()  # "good" was started, then reaped.


class SpawnPipelineTest(Contract.Pipeline):
    start_method = "spawn"


class ForkPipelineTest(Contract.Pipeline):
    start_method = "fork"
    pickles_sources = False


class ForkserverPipelineTest(Contract.Pipeline):
    start_method = "forkserver"


class StartupSignalTest(WatchdogTestCase):
    def test_signals_during_start_up_are_deferred_not_lost(self) -> None:
        # Producers start inside this block and inherit the starting thread's
        # mask, so a Ctrl+C mid-start cannot kill a half-started child. The
        # parent must still see the signal once every child is up. The
        # signal targets this thread: the mask is per thread, and the
        # watchdog's thread would otherwise take a process-wide signal.
        reached_end_of_block = False
        with (
            self.assertRaises(KeyboardInterrupt),
            piper._coordinated_signals_deferred(),
        ):
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)
            time.sleep(0.05)  # Ample time for an undeferred delivery.
            reached_end_of_block = True
        self.assertTrue(reached_end_of_block)


class ValidationTest(WatchdogTestCase):
    def test_rejects_invalid_options_before_starting_anything(self) -> None:
        cases: list[dict[str, Any]] = [
            {"workers": 0},
            {"workers": -1},
            {"max_in_flight": 0},
            {"batch_size": 0},
            {"start_method": "threads"},
        ]
        source = functools.partial(numbers, 1)
        for options in cases:
            with self.subTest(**options), self.assertRaises(ValueError):
                run_pipeline({"p": source}, noop, **options)
        self.assertEqual(multiprocessing.active_children(), [])


if __name__ == "__main__":
    unittest.main()
