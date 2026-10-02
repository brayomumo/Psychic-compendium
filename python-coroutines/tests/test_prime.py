"""Tests for the priming decorator and the forward() helper."""

import inspect
import unittest
from collections.abc import Generator

from prime import coroutine, forward


def echo() -> Generator[str, str, None]:
    """Yield back whatever was sent."""
    received = ""
    while True:
        received = yield received


class CoroutineDecoratorTest(unittest.TestCase):
    def test_unprimed_generator_rejects_non_none_send(self) -> None:
        gen = echo()
        with self.assertRaisesRegex(TypeError, "just-started generator"):
            gen.send("hello")

    def test_primed_generator_is_suspended_and_accepts_send(self) -> None:
        gen = coroutine(echo)()
        self.assertEqual(inspect.getgeneratorstate(gen), inspect.GEN_SUSPENDED)
        self.assertEqual(gen.send("hello"), "hello")

    def test_preserves_name_doc_and_wrapped(self) -> None:
        primed = coroutine(echo)
        self.assertEqual(primed.__name__, "echo")
        self.assertEqual(primed.__doc__, echo.__doc__)
        self.assertIs(getattr(primed, "__wrapped__", None), echo)

    def test_validation_before_first_yield_runs_at_construction(self) -> None:
        @coroutine
        def needs_positive(n: int) -> Generator[None, int, None]:
            if n <= 0:
                raise ValueError("n must be positive")
            while True:
                yield

        with self.assertRaisesRegex(ValueError, "positive"):
            needs_positive(0)

    def test_generator_that_returns_before_yield_gets_clear_error(
        self,
    ) -> None:
        @coroutine
        def stops_early(run: bool) -> Generator[None, int, None]:
            while run:
                yield

        with self.assertRaisesRegex(RuntimeError, "before its first yield"):
            stops_early(False)
        try:
            stops_early(False)
        except RuntimeError as exc:
            self.assertIsInstance(exc.__cause__, StopIteration)


class ForwardTest(unittest.TestCase):
    def test_returns_true_while_target_runs(self) -> None:
        received: list[int] = []

        @coroutine
        def sink() -> Generator[None, int, None]:
            while True:
                received.append((yield))

        self.assertTrue(forward(sink(), 1))
        self.assertEqual(received, [1])

    def test_returns_false_once_target_finished(self) -> None:
        @coroutine
        def one_shot() -> Generator[None, int, None]:
            yield

        self.assertFalse(forward(one_shot(), 1))

    def test_returns_false_for_closed_target(self) -> None:
        @coroutine
        def sink() -> Generator[None, int, None]:
            while True:
                yield

        gen = sink()
        gen.close()
        self.assertFalse(forward(gen, 1))

    def test_propagates_other_exceptions(self) -> None:
        @coroutine
        def broken() -> Generator[None, int, None]:
            yield
            raise KeyError("boom")

        with self.assertRaises(KeyError):
            forward(broken(), 1)


if __name__ == "__main__":
    unittest.main()
