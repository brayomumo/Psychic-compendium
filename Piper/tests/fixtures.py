"""Test-only producers, at module level so that spawn children import them."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from multiprocessing.sharedctypes import Synchronized


def counted_blobs(
    produced: Synchronized[int], count: int, size: int
) -> Iterator[bytes]:
    """Yields ``count`` blobs, counting each in shared memory first."""
    for _ in range(count):
        with produced.get_lock():
            produced.value += 1
        yield bytes(size)


def endless_pids() -> Iterator[int]:
    """Yields this process's pid forever."""
    pid = os.getpid()
    while True:
        yield pid


def numbers_with_sigint(count: int) -> Iterator[int]:
    """Yields ``0 .. count-1``, sending itself SIGINT halfway through."""
    for value in range(count):
        if value == count // 2:
            os.kill(os.getpid(), signal.SIGINT)
        yield value


def fork_pipe_holder_then_die(hold_s: float) -> Iterator[int]:
    """Leaves a grandchild holding the pipe open, then dies without a word.

    The forked grandchild inherits the write end, so the consumer will not
    see EOF while it lives. Yields the grandchild's pid so the test can
    clean it up.
    """
    holder = os.fork()
    if holder == 0:
        time.sleep(hold_s)
        os._exit(0)
    yield holder
    os.kill(os.getpid(), signal.SIGKILL)
