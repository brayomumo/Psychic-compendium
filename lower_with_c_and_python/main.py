"""Walks through calling C from Python with ctypes. See README.md.

Usage:
    python3 main.py                      # short walkthrough, exits 0
    python3 main.py --until-interrupted  # long C loop; Ctrl+C exits 130

Exit codes: 0 success, 1 runtime failure (e.g. library not built), 2 usage
error, 130 interrupted by Ctrl+C.
"""

import argparse
import ctypes
import logging
import sys

import csum

logger = logging.getLogger("c-from-python")

_REPORT_EVERY = 10_000_000
_STOP_AT_PARTIAL = 20
_FAILING_INDEX = 3


def _show(label: str, value: object) -> None:
    print(f"  {label:<38} {value}")


def _walkthrough() -> None:
    print("1. The original demo, fixed: sum(range(0, 100)) in C")
    _show("sum_range(0, 100)", csum.sum_range(0, 100))

    print("2. Control crosses back into Python through a callback")
    csum.sum_range(
        0, 4, lambda i, partial: _show(f"progress(index={i})", partial)
    )

    print("3. The callback can stop C early")
    try:
        csum.sum_range(0, 100, lambda _i, partial: partial >= _STOP_AT_PARTIAL)
    except csum.SumStoppedError as exc:
        _show(
            f"stop once partial >= {_STOP_AT_PARTIAL}",
            f"SumStoppedError({exc.partial})",
        )

    print("4. C status codes and bad input become exceptions")
    for label, start, stop in [
        (
            "sum_range(INT64_MAX - 2, INT64_MAX)",
            csum.INT64_MAX - 2,
            csum.INT64_MAX,
        ),
        ("sum_range(0, 2**64)", 0, 2**64),
        ("sum_range(0, 1.5)", 0, 1.5),
    ]:
        try:
            csum.sum_range(start, stop)  # type: ignore[arg-type]
        except (OverflowError, TypeError) as exc:
            _show(label, f"{type(exc).__name__}: {exc}")
    _show(
        "why: ctypes.c_int64(2**64 + 5).value", ctypes.c_int64(2**64 + 5).value
    )

    print(f"5. A callback that raises at index {_FAILING_INDEX}")
    _raw_ctypes_swallows_exception()
    calls = 0

    def failing(index: int, _partial: int) -> None:
        nonlocal calls
        calls += 1
        if index == _FAILING_INDEX:
            raise ValueError(f"bad element {index}")

    try:
        csum.sum_range(0, 10, failing)
    except ValueError as exc:
        _show("wrapper", f"{exc!r} re-raised after {calls} callbacks")


def _raw_ctypes_swallows_exception() -> None:
    """Calls the C function directly to show what the wrapper protects from."""
    lib = csum.load_library(csum.library_path())
    calls = 0
    reported: list[str] = []

    def failing(index: int, _partial: int, _data: int | None) -> int:
        nonlocal calls
        calls += 1
        if index == _FAILING_INDEX:
            raise ValueError(f"bad element {index}")
        return 0

    callback = csum.CProgressFn(failing)
    result = ctypes.c_int64()
    previous_hook = sys.unraisablehook
    sys.unraisablehook = lambda u: reported.append(repr(u.exc_value))
    try:
        status = lib.sum_range(0, 10, callback, None, ctypes.byref(result))
    finally:
        sys.unraisablehook = previous_hook
    _show("raw ctypes: reported as unraisable", ", ".join(reported))
    _show(
        "raw ctypes: C kept going",
        f"status={status} result={result.value} after {calls} callbacks",
    )


def _until_interrupted() -> None:
    def report(index: int, partial: int) -> None:
        if index % _REPORT_EVERY == 0:
            logger.info("index=%d partial=%d", index, partial)

    logger.info("summing in C with a progress callback; press Ctrl+C to stop")
    try:
        csum.sum_range(0, csum.INT64_MAX, report)
    except csum.SumOverflowError as exc:
        print(exc)


def main(argv: list[str] | None = None) -> int:
    """Runs the demo and returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--until-interrupted",
        action="store_true",
        help="run a long C loop with callbacks until Ctrl+C",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        csum.load_library(csum.library_path())  # Fail before any output.
        if args.until_interrupted:
            _until_interrupted()
        else:
            _walkthrough()
    except csum.LibraryLoadError as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("interrupted; C stopped at the next callback")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
