"""Tests for the coroutine pipeline: delegation, close propagation, throw."""

import contextlib
import inspect
import io
import itertools
import unittest
from collections.abc import Generator, Iterator
from typing import Any

import pipeline
from pipeline import Flush, FrameError
from prime import Sink


def closed(*gens: Generator[Any, Any, Any]) -> bool:
    """Whether every generator is closed."""
    return all(inspect.getgeneratorstate(g) == inspect.GEN_CLOSED for g in gens)


class Stages:
    """The demo pipeline, built bottom-up so every stage can be inspected."""

    def __init__(self, batch_size: int = 2) -> None:
        self.batches: list[list[str]] = []
        self.sink = pipeline.collect(self.batches)
        self.batcher = pipeline.batch(batch_size, self.sink)
        self.selector = pipeline.select(bool, self.batcher)
        self.joiner = pipeline.transform(" ".join, self.selector)
        self.head = pipeline.deframe(self.joiner)

    def all(self) -> list[Generator[Any, Any, Any]]:
        return [self.head, self.joiner, self.selector, self.batcher, self.sink]


class EndToEndTest(unittest.TestCase):
    def test_phrase_pipeline_batches_complete_frames(self) -> None:
        stages = Stages()
        tokens = ["3", "a", "b", "c", "0", "2", "d", "e", "1", "f"]
        pipeline.feed(tokens, stages.head)
        self.assertEqual(stages.batches, [["a b c", "d e"], ["f"]])

    def test_close_propagates_through_every_stage(self) -> None:
        stages = Stages()
        pipeline.feed(["1", "a"], stages.head)
        self.assertTrue(closed(*stages.all()))

    def test_truncated_final_frame_is_dropped_with_warning(self) -> None:
        stages = Stages()
        with self.assertLogs("pipeline", "WARNING") as logs:
            pipeline.feed(["1", "a", "3", "b", "c"], stages.head)
        self.assertEqual(stages.batches, [["a"]])
        self.assertIn("got 2 of 3 tokens", logs.output[0])

    def test_close_between_frames_does_not_warn(self) -> None:
        stages = Stages()
        with self.assertNoLogs("pipeline", "WARNING"):
            pipeline.feed(["1", "a"], stages.head)

    def test_malformed_header_raises_and_closes_every_stage(self) -> None:
        stages = Stages()
        with self.assertRaisesRegex(FrameError, "must be an integer"):
            pipeline.feed(["1", "a", "x"], stages.head)
        self.assertTrue(closed(*stages.all()))
        # Downstream was closed normally, so the partial batch was flushed.
        self.assertEqual(stages.batches, [["a"]])

    def test_source_crash_closes_every_stage_and_propagates(self) -> None:
        def crashing_source() -> Iterator[str]:
            yield from ["1", "a"]
            raise ConnectionResetError("source went away")

        stages = Stages()
        with self.assertRaises(ConnectionResetError):
            pipeline.feed(crashing_source(), stages.head)
        self.assertTrue(closed(*stages.all()))
        self.assertEqual(stages.batches, [["a"]])

    def test_negative_frame_length_is_rejected(self) -> None:
        with self.assertRaisesRegex(FrameError, ">= 0"):
            pipeline.feed(["-1"], Stages().head)

    def test_stage_exception_closes_downstream_and_propagates(self) -> None:
        def invert(x: int) -> int:
            return 1 // x

        batches: list[list[int]] = []
        sink = pipeline.collect(batches)
        batcher = pipeline.batch(10, sink)
        inverter = pipeline.transform(invert, batcher)
        with self.assertRaises(ZeroDivisionError):
            pipeline.feed([1, 1, 0, 1], inverter)
        self.assertTrue(closed(inverter, batcher, sink))
        self.assertEqual(batches, [[1, 1]])


class DelegationTest(unittest.TestCase):
    def test_subgenerator_return_value_arrives_in_stop_iteration(self) -> None:
        frame = pipeline.read_frame()
        next(frame)
        frame.send("2")
        frame.send("a")
        with self.assertRaises(StopIteration) as stop:
            frame.send("b")
        self.assertEqual(stop.exception.value, ("a", "b"))

    def test_yield_from_hands_return_value_to_delegator(self) -> None:
        frames: list[tuple[str, ...]] = []
        head = pipeline.deframe(pipeline.collect(frames))
        pipeline.feed(["2", "a", "b", "0", "1", "c"], head)
        self.assertEqual(frames, [("a", "b"), (), ("c",)])

    def test_preprimed_subgenerator_breaks_yield_from(self) -> None:
        frame = pipeline.read_frame()
        next(frame)  # wrong: yield from primes it again with None

        def delegator() -> Generator[None, str, tuple[str, ...]]:
            return (yield from frame)

        with self.assertRaisesRegex(TypeError, "NoneType"):
            next(delegator())


class ThrowTest(unittest.TestCase):
    def test_flush_emits_partial_batch_and_keeps_running(self) -> None:
        batches: list[list[int]] = []
        batcher = pipeline.batch(3, pipeline.collect(batches))
        batcher.send(1)
        self.assertIsNone(batcher.throw(Flush()))
        self.assertEqual(batches, [[1]])
        self.assertEqual(
            inspect.getgeneratorstate(batcher), inspect.GEN_SUSPENDED
        )
        for item in (2, 3, 4):
            batcher.send(item)
        self.assertEqual(batches, [[1], [2, 3, 4]])

    def test_flush_with_nothing_pending_emits_nothing(self) -> None:
        batches: list[list[int]] = []
        batcher = pipeline.batch(3, pipeline.collect(batches))
        batcher.throw(Flush())
        self.assertEqual(batches, [])

    def test_unhandled_throw_propagates_and_closes_downstream(self) -> None:
        batches: list[list[int]] = []
        sink = pipeline.collect(batches)
        batcher = pipeline.batch(3, sink)
        batcher.send(1)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            batcher.throw(RuntimeError("boom"))
        self.assertTrue(closed(batcher, sink))
        # A crash is not a normal close: the partial batch is discarded.
        self.assertEqual(batches, [])

    def test_close_flushes_partial_batch(self) -> None:
        batches: list[list[int]] = []
        batcher = pipeline.batch(3, pipeline.collect(batches))
        pipeline.feed([1, 2, 3, 4], batcher)
        self.assertEqual(batches, [[1, 2, 3], [4]])


class EarlyStopTest(unittest.TestCase):
    def test_take_stops_upstream_pulling_from_infinite_source(self) -> None:
        source = itertools.count()
        taken: list[int] = []
        pipeline.feed(source, pipeline.take(3, pipeline.collect(taken)))
        self.assertEqual(taken, [0, 1, 2])
        self.assertEqual(next(source), 3, "pulled more than it needed")

    def test_finished_downstream_stops_upstream_without_pep479_error(
        self,
    ) -> None:
        def double(x: int) -> int:
            return x * 2

        taken: list[int] = []
        sink = pipeline.collect(taken)
        limiter = pipeline.take(2, sink)
        doubler = pipeline.transform(double, limiter)
        pipeline.feed(itertools.count(), doubler)
        self.assertEqual(taken, [0, 2])
        self.assertTrue(closed(doubler, limiter, sink))

    def test_take_zero_forwards_nothing(self) -> None:
        source = iter(range(10))
        taken: list[int] = []
        pipeline.feed(source, pipeline.take(0, pipeline.collect(taken)))
        self.assertEqual(taken, [])
        self.assertEqual(next(source), 1, "consumes one item to refuse it")

    def test_batch_downstream_finished_stops_batcher(self) -> None:
        taken: list[list[int]] = []
        batcher = pipeline.batch(2, pipeline.take(1, pipeline.collect(taken)))
        pipeline.feed(itertools.count(), batcher)
        self.assertEqual(taken, [[0, 1]])


class ValidationTest(unittest.TestCase):
    def test_invalid_batch_size_fails_fast_and_closes_target(self) -> None:
        for size in (0, -1):
            with self.subTest(size=size):
                target: Sink[list[int]] = pipeline.collect([])
                with self.assertRaisesRegex(ValueError, "batch size"):
                    pipeline.batch(size, target)
                self.assertTrue(closed(target))

    def test_negative_take_limit_fails_fast_and_closes_target(self) -> None:
        target: Sink[int] = pipeline.collect([])
        with self.assertRaisesRegex(ValueError, "limit"):
            pipeline.take(-1, target)
        self.assertTrue(closed(target))

    def test_select_filters(self) -> None:
        kept: list[int] = []
        evens = pipeline.select(lambda x: x % 2 == 0, pipeline.collect(kept))
        pipeline.feed(range(7), evens)
        self.assertEqual(kept, [0, 2, 4, 6])

    def test_empty_source_still_closes_pipeline(self) -> None:
        stages = Stages()
        pipeline.feed([], stages.head)
        self.assertTrue(closed(*stages.all()))
        self.assertEqual(stages.batches, [])


class MainTest(unittest.TestCase):
    def test_demo_runs_and_exits_zero(self) -> None:
        out = io.StringIO()
        # Capturing on the root logger also keeps main()'s basicConfig from
        # installing a handler that would outlive this test.
        with contextlib.redirect_stdout(out), self.assertLogs(level="WARNING"):
            self.assertEqual(pipeline.main([]), 0)
        self.assertIn("batch 2: ['the', 'lazy dog']", out.getvalue())


if __name__ == "__main__":
    unittest.main()
