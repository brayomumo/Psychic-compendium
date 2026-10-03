"""What cProfile cannot see, demonstrated rather than asserted.

* **Other processes.** A profiler lives in one interpreter. Work a
  multiprocessing child does never reaches the parent's profile, under any
  start method: under ``fork`` the child records into its own *copy* of the
  profiler, which dies with it. The fix is to profile inside each child,
  save a ``.prof`` file per child, and merge them with ``pstats.Stats.add``.
* **Other threads, depending on the Python version.**

  - Up to 3.11, cProfile hooks ``sys.setprofile``, which is per thread: it
    sees only the thread that enabled it. The recipe was one ``Profile`` per
    thread.
  - From 3.12, cProfile is built on ``sys.monitoring``, which is global. It
    records calls from every thread, but into one call stack, so time is
    charged to whichever function is on top of that stack, whatever thread
    is running. Calls are counted; per-thread times are wrong. And only one
    profiler may be active per interpreter, so the per-thread recipe now
    raises ``ValueError``.
"""

import cProfile
import multiprocessing
import pstats
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from multiprocessing.context import (
    ForkContext,
    ForkServerContext,
    SpawnContext,
)
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Literal

__all__ = [
    "MAX_CHILDREN",
    "START_METHODS",
    "StartMethod",
    "ThreadFinding",
    "child_hotspot",
    "parent_sees_child",
    "profile_children",
    "thread_finding",
]

MAX_CHILDREN = 16

StartMethod = Literal["spawn", "fork", "forkserver"]
START_METHODS: tuple[StartMethod, ...] = ("spawn", "fork", "forkserver")


def _context(
    start_method: StartMethod,
) -> SpawnContext | ForkContext | ForkServerContext:
    """Returns the concrete context, which is what declares ``Process``."""
    if start_method == "spawn":
        return multiprocessing.get_context("spawn")
    if start_method == "fork":
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context("forkserver")


def child_hotspot(n: int) -> int:
    """CPU work done in a child process. Module-level, so spawn can import it.

    Args:
        n: Loop count.

    Returns:
        The sum of squares below ``n``.
    """
    total = 0
    for i in range(n):
        total += i * i
    return total


def _worker_hotspot(n: int) -> int:
    total = 0
    for i in range(n):
        total += i * i
    return total


def _main_hotspot(n: int) -> int:
    total = 0
    for i in range(n):
        total += i * i
    return total


def _names(stats: pstats.Stats) -> set[str]:
    return set(stats.get_stats_profile().func_profiles)


def parent_sees_child(start_method: StartMethod, n: int) -> bool:
    """Profiles a parent while a child process does the work.

    Args:
        start_method: ``"spawn"``, ``"fork"`` or ``"forkserver"``.
        n: Work for the child.

    Returns:
        Whether ``child_hotspot`` appears in the parent's profile. It never
        does.

    Raises:
        RuntimeError: The child failed.
    """
    context = _context(start_method)
    process = context.Process(target=child_hotspot, args=(n,), daemon=True)
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        process.start()
        process.join()
    finally:
        profiler.disable()
        _stop([process])
    if process.exitcode != 0:
        raise RuntimeError(f"child exited with {process.exitcode}")
    return "child_hotspot" in _names(pstats.Stats(profiler))


def _profile_in_child(path: str, n: int) -> None:
    """Child entry point: profile the work here, where it happens."""
    profiler = cProfile.Profile()
    profiler.runcall(child_hotspot, n)
    profiler.dump_stats(path)


def profile_children(
    start_method: StartMethod, children: int, n: int, out_dir: Path
) -> pstats.Stats:
    """Profiles inside each child and merges the per-child profiles.

    The parent must not have a profiler active: under ``fork`` on 3.12+ the
    child inherits it, and enabling another one there raises ``ValueError``.

    Args:
        start_method: ``"spawn"``, ``"fork"`` or ``"forkserver"``.
        children: Number of child processes, in ``1..MAX_CHILDREN``.
        n: Work per child.
        out_dir: Where each child writes ``child-<i>.prof``.

    Returns:
        The merged statistics, in which ``child_hotspot`` has one call per
        child.

    Raises:
        ValueError: ``children`` is out of range.
        RuntimeError: A child failed.
    """
    if not 1 <= children <= MAX_CHILDREN:
        raise ValueError(
            f"children must be in 1..{MAX_CHILDREN}, got {children}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    context = _context(start_method)
    paths = [out_dir / f"child-{i}.prof" for i in range(children)]
    processes = [
        context.Process(
            target=_profile_in_child, args=(str(path), n), daemon=True
        )
        for path in paths
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join()
    finally:
        _stop(processes)
    failed = [p.exitcode for p in processes if p.exitcode != 0]
    if failed:
        raise RuntimeError(f"children failed with exit codes {failed}")
    merged = pstats.Stats(str(paths[0]))
    merged.add(*(str(path) for path in paths[1:]))
    return merged


def _stop(processes: Sequence[BaseProcess]) -> None:
    """Terminates and reaps any child still running (after an interrupt)."""
    for process in processes:
        if process.pid is not None and process.is_alive():
            process.terminate()
    for process in processes:
        if process.pid is not None:
            process.join()


@dataclass(frozen=True)
class ThreadFinding:
    """What cProfile reported about a second thread, versus the truth.

    Attributes:
        python: ``major.minor`` of the interpreter.
        worker_seen: Whether the worker thread's function was recorded.
        worker_reported: Seconds cProfile charged to the worker's function
            (0 when unseen).
        worker_cpu: CPU seconds the worker thread actually used.
        main_reported: Seconds cProfile charged to the main thread's
            function.
        main_cpu: CPU seconds the main thread's function actually used.
        second_profiler_error: The error from enabling a second profiler in
            another thread while one is active, or ``None`` if allowed.
    """

    python: str
    worker_seen: bool
    worker_reported: float
    worker_cpu: float
    main_reported: float
    main_cpu: float
    second_profiler_error: str | None


def thread_finding(n: int) -> ThreadFinding:
    """Profiles the main thread while another thread does the same work.

    Args:
        n: Work per thread.

    Returns:
        What cProfile reported, next to each thread's measured CPU time.
    """
    worker_cpu: list[float] = []

    def worker() -> None:
        start = time.thread_time()
        _worker_hotspot(n)
        worker_cpu.append(time.thread_time() - start)

    thread = threading.Thread(target=worker, name="worker")
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        thread.start()
        start = time.thread_time()
        _main_hotspot(n)
        main_cpu = time.thread_time() - start
        thread.join()
    finally:
        profiler.disable()
    profiles = pstats.Stats(profiler).get_stats_profile().func_profiles
    seen = profiles.get("_worker_hotspot")
    main = profiles.get("_main_hotspot")
    return ThreadFinding(
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
        worker_seen=seen is not None,
        worker_reported=seen.tottime if seen else 0.0,
        worker_cpu=worker_cpu[0],
        main_reported=main.tottime if main else 0.0,
        main_cpu=main_cpu,
        second_profiler_error=_second_profiler_error(),
    )


def _second_profiler_error() -> str | None:
    errors: list[str] = []

    def per_thread_recipe() -> None:
        inner = cProfile.Profile()
        try:
            inner.enable()
        except ValueError as exc:
            errors.append(str(exc))
            return
        inner.disable()

    outer = cProfile.Profile()
    outer.enable()
    try:
        thread = threading.Thread(target=per_thread_recipe)
        thread.start()
        thread.join()
    finally:
        outer.disable()
    return errors[0] if errors else None
