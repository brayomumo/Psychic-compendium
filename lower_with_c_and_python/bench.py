"""Measures two properties of the ctypes boundary that README.md relies on.

1. The cost of one C -> Python callback round trip.
2. That ctypes.CDLL releases the GIL during a call: two threads running a
   long C loop finish in about the time of one, while the same library loaded
   with ctypes.PyDLL (which holds the GIL) takes about twice as long.

Each measurement is repeated and the median is reported.
"""

import argparse
import ctypes
import functools
import os
import platform
import statistics
import sys
import threading
import time
from collections.abc import Callable

import csum


def _median_seconds(fn: Callable[[], object], repeats: int) -> float:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _callback_cost(elements: int, repeats: int) -> None:
    def noop(_index: int, _partial: int) -> None:
        return None

    with_cb = _median_seconds(
        lambda: csum.sum_range(0, elements, noop), repeats
    )
    without = _median_seconds(lambda: csum.sum_range(0, elements), repeats)
    per_call_ns = (with_cb - without) / elements * 1e9
    print(f"callback round trip: {per_call_ns:.0f} ns per call")
    print(
        f"  sum_range(0, {elements:_}) with no-op callback {with_cb:.3f} s, "
        f"without {without * 1e3:.1f} ms"
    )


def _raw_call(lib: ctypes.CDLL, elements: int) -> None:
    result = ctypes.c_int64()
    status = lib.sum_range(
        0, elements, csum.CProgressFn(), None, ctypes.byref(result)
    )
    if status != 0:
        raise RuntimeError(f"sum_range failed with status {status}")


def _two_threads(lib: ctypes.CDLL, elements: int) -> None:
    threads = [
        threading.Thread(target=_raw_call, args=(lib, elements))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _gil_release(elements: int, repeats: int) -> None:
    path = str(csum.library_path())
    print(f"GIL: 2 threads each summing range(0, {elements:_}), no callback")
    for name, lib in [
        ("CDLL  (releases GIL)", ctypes.CDLL(path)),
        ("PyDLL (holds GIL)   ", ctypes.PyDLL(path)),
    ]:
        csum.declare_signatures(lib)
        one = _median_seconds(
            functools.partial(_raw_call, lib, elements), repeats
        )
        two = _median_seconds(
            functools.partial(_two_threads, lib, elements), repeats
        )
        print(
            f"  {name} one call {one:.3f} s, two threads {two:.3f} s "
            f"-> {two / one:.2f}x the time of one"
        )


def main(argv: list[str] | None = None) -> int:
    """Runs both benchmarks and returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--callback-elements", type=int, default=1_000_000)
    parser.add_argument("--gil-elements", type=int, default=500_000_000)
    args = parser.parse_args(argv)
    if min(args.repeats, args.callback_elements, args.gil_elements) < 1:
        parser.error("all values must be >= 1")

    print(
        f"{platform.machine()} {platform.system()} {platform.release()}, "
        f"{os.cpu_count()} CPUs, Python {platform.python_version()}, "
        f"median of {args.repeats}"
    )
    _callback_cost(args.callback_elements, args.repeats)
    _gil_release(args.gil_elements, args.repeats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
