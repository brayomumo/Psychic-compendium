"""What can cross a process boundary: whatever multiprocessing can pickle."""

from __future__ import annotations

import functools
import os
import pickle
import threading
import unittest
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from multiprocessing.reduction import ForkingPickler
from typing import TypeVar, cast

from workloads import Record, records

PICKLE_ERRORS = (pickle.PicklingError, TypeError, AttributeError)
T = TypeVar("T")


def round_trip(obj: T) -> T:
    return cast(T, pickle.loads(ForkingPickler.dumps(obj)))


class ProcessBoundaryTest(unittest.TestCase):
    def test_plain_data_crosses_intact_even_when_json_cannot(self) -> None:
        now = datetime.now(UTC)
        cases: list[object] = [
            Record("p", 1, now, Decimal("1.10")),  # The "complex" object.
            now,
            Decimal("1.10"),
            {"nested": [1, (2, 3)], "bytes": b"\x00"},
        ]
        for obj in cases:
            with self.subTest(type=type(obj).__name__):
                self.assertEqual(round_trip(obj), obj)

    def test_module_level_functions_cross_by_reference(self) -> None:
        source = functools.partial(records, "p", 2)
        rebuilt = round_trip(source)
        self.assertEqual([r.index for r in rebuilt()], [0, 1])

    def test_live_resources_and_anonymous_code_do_not(self) -> None:
        def local_function() -> None:
            pass

        def generator() -> Iterator[int]:
            yield 1

        with open(os.devnull) as open_file:
            cases: dict[str, object] = {
                "lock": threading.Lock(),
                "lambda": lambda: None,
                "local function": local_function,
                "generator": generator(),
                "open file": open_file,
            }
            for name, obj in cases.items():
                with self.subTest(name), self.assertRaises(PICKLE_ERRORS):
                    ForkingPickler.dumps(obj)


if __name__ == "__main__":
    unittest.main()
