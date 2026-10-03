import functools
import subprocess
import sys
import tracemalloc
from pathlib import Path

import memory
import workload
from support import PROTOTYPE_DIR, WatchdogTestCase, line_of

LEAK_LINE = line_of(PROTOTYPE_DIR / "workload.py", "# LEAK:")
KEYS = 500


class LeakTest(WatchdogTestCase):
    def setUp(self) -> None:
        super().setUp()
        workload.forget()
        self.addCleanup(workload.forget)

    def test_tracemalloc_points_at_the_leaking_line(self) -> None:
        growth = memory.find_growth(functools.partial(workload.leak, KEYS))
        top = growth[0]
        self.assertEqual(Path(top.filename).name, "workload.py")
        self.assertEqual(top.lineno, LEAK_LINE)
        self.assertEqual(top.blocks, KEYS)
        self.assertGreaterEqual(top.size_bytes, KEYS * workload.PAYLOAD_BYTES)
        self.assertIn("bytes(PAYLOAD_BYTES)", top.source)

    def test_filter_compilation_noise_bug_reports_only_the_workload(
        self,
    ) -> None:
        # Filtering between the snapshots once reported fnmatch and re
        # internals as growth: compiling the filter patterns allocates. The
        # compiled patterns are cached for the life of the process, so only
        # a fresh interpreter (cold caches) shows the bug.
        script = (
            "import functools, memory, workload\n"
            f"growth = memory.find_growth("
            f"functools.partial(workload.leak, {KEYS}), limit=20)\n"
            "print('\\n'.join(sorted({g.filename for g in growth})))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROTOTYPE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        files = {Path(line).name for line in result.stdout.splitlines()}
        self.assertEqual(files, {"workload.py"})

    def test_cache_hits_allocate_nothing_new(self) -> None:
        workload.leak(KEYS)
        # Same keys again: every lookup hits, so the payload line is quiet.
        growth = memory.find_growth(
            lambda: [workload.remember(k) for k in range(KEYS)]
        )
        self.assertNotIn(LEAK_LINE, [g.lineno for g in growth])

    def test_freed_memory_is_not_reported_as_growth(self) -> None:
        # Allocated and dropped inside the measurement: no net growth.
        growth = memory.find_growth(
            lambda: [bytes(workload.PAYLOAD_BYTES) for _ in range(KEYS)]
        )
        self.assertTrue(all(g.size_bytes < KEYS for g in growth))


class TracingStateTest(WatchdogTestCase):
    def test_tracing_is_stopped_when_it_was_off(self) -> None:
        self.assertFalse(tracemalloc.is_tracing())
        memory.find_growth(lambda: None)
        self.assertFalse(tracemalloc.is_tracing())

    def test_tracing_is_left_on_when_it_was_on(self) -> None:
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        memory.find_growth(lambda: None)
        self.assertTrue(tracemalloc.is_tracing())

    def test_invalid_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            memory.find_growth(lambda: None, limit=0)

    def test_table_has_one_line_per_row_plus_header(self) -> None:
        workload.forget()
        self.addCleanup(workload.forget)
        rows = memory.find_growth(functools.partial(workload.leak, 10))
        self.assertEqual(
            len(memory.format_table(rows).splitlines()), len(rows) + 1
        )
