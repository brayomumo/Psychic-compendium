"""Tests for the shared command-line helpers."""

import argparse
import contextlib
import io
import os
import signal
import time
import unittest
from collections.abc import Callable

import cli


class BoundedIntTest(unittest.TestCase):
    def test_accepts_values_inside_the_range(self) -> None:
        parse = cli.bounded_int(1, 10)
        self.assertEqual([parse("1"), parse("10"), parse(" 5 ")], [1, 10, 5])

    def test_rejects_out_of_range_and_non_integers(self) -> None:
        parse = cli.bounded_int(1, 10)
        for text in ("0", "11", "-1", "abc", "1.5", ""):
            with (
                self.subTest(text=text),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                parse(text)


class BoundedSecondsTest(unittest.TestCase):
    def test_accepts_finite_values_inside_the_range(self) -> None:
        parse = cli.bounded_seconds(60)
        self.assertEqual(
            [parse("0"), parse("0.25"), parse("60")], [0, 0.25, 60]
        )

    def test_rejects_negative_infinite_nan_and_too_large(self) -> None:
        parse = cli.bounded_seconds(60)
        for text in ("-0.1", "inf", "-inf", "nan", "60.01", "soon"):
            with (
                self.subTest(text=text),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                parse(text)


class RunInterruptibleTest(unittest.TestCase):
    def run_quietly(self, work: Callable[[], object]) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return cli.run_interruptible(work, on_interrupt="stopped")

    def test_returns_zero_when_work_completes(self) -> None:
        self.assertEqual(self.run_quietly(lambda: None), 0)

    def test_sigint_returns_130(self) -> None:
        def interrupted() -> None:
            raise KeyboardInterrupt

        self.assertEqual(self.run_quietly(interrupted), 130)

    def test_sigterm_returns_143_and_restores_handler(self) -> None:
        def terminated() -> None:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)  # never completes: the handler fires first

        before = signal.getsignal(signal.SIGTERM)
        self.assertEqual(self.run_quietly(terminated), 143)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_other_errors_propagate_and_handler_is_restored(self) -> None:
        before = signal.getsignal(signal.SIGTERM)

        def broken() -> None:
            raise OSError("disk full")

        with self.assertRaisesRegex(OSError, "disk full"):
            self.run_quietly(broken)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


if __name__ == "__main__":
    unittest.main()
