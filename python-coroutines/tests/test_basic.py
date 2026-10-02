"""Tests for the send/close/throw basics."""

import contextlib
import inspect
import io
import unittest

import basic


class GrepTest(unittest.TestCase):
    def test_reports_only_matching_lines(self) -> None:
        matches: list[str] = []
        finder = basic.grep("brian", matches.append)
        for line in ["brian 1", "nobody", "hi brian", ""]:
            finder.send(line)
        self.assertEqual(matches, ["brian 1", "hi brian"])

    def test_yield_str_bug_send_returns_none_not_the_str_type(self) -> None:
        # The first version wrote `line = yield str`, so every send()
        # returned the built-in `str` type instead of nothing.
        finder = basic.grep("x", lambda _: None)
        self.assertIsNone(finder.send("x"))
        self.assertIsNone(finder.send("y"))

    def test_close_then_send_raises_stop_iteration(self) -> None:
        finder = basic.grep("x", lambda _: None)
        finder.close()
        self.assertEqual(inspect.getgeneratorstate(finder), inspect.GEN_CLOSED)
        with self.assertRaises(StopIteration):
            finder.send("x")

    def test_close_is_idempotent(self) -> None:
        finder = basic.grep("x", lambda _: None)
        finder.close()
        finder.close()
        self.assertEqual(inspect.getgeneratorstate(finder), inspect.GEN_CLOSED)


class RunningAverageTest(unittest.TestCase):
    def test_send_returns_mean_so_far(self) -> None:
        averager = basic.running_average()
        self.assertEqual(
            [averager.send(x) for x in (10.0, 20.0, 60.0)], [10.0, 15.0, 30.0]
        )

    def test_unhandled_throw_reraises_and_closes(self) -> None:
        averager = basic.running_average()
        with self.assertRaisesRegex(ValueError, "injected"):
            averager.throw(ValueError("injected"))
        self.assertEqual(
            inspect.getgeneratorstate(averager), inspect.GEN_CLOSED
        )


class MainTest(unittest.TestCase):
    def test_demo_runs_and_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(basic.main([]), 0)
        self.assertIn("GEN_CLOSED", out.getvalue())
        self.assertIn("just-started generator", out.getvalue())


if __name__ == "__main__":
    unittest.main()
