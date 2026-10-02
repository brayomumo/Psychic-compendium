"""Runs the entry points as real processes and watches their output."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

PROJECT_DIR = Path(__file__).resolve().parent.parent
TIMEOUT_S = 30.0
_EXEC_WITH_SIGINT_IGNORED = (
    "import os, signal, sys;"
    " signal.signal(signal.SIGINT, signal.SIG_IGN);"
    " os.execv(sys.argv[1], sys.argv[1:])"
)


def child_env(overrides: Mapping[str, str]) -> dict[str, str]:
    """The current environment minus our settings, plus ``overrides``.

    Dropping inherited PUBSUB_* and RABBITMQ_* variables keeps a developer's
    shell from changing what a test exercises.
    """
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PUBSUB_", "RABBITMQ_"))
    }
    return {**base, **overrides}


class Proc:
    """A child process whose stdout and stderr lines are collected live."""

    def __init__(
        self,
        module: str,
        env: Mapping[str, str],
        *,
        sigint_ignored: bool = False,
    ) -> None:
        """Starts ``python -m module``.

        Args:
            module: the entry point to run.
            env: overrides on top of the sanitised environment.
            sigint_ignored: start the child with SIGINT set to SIG_IGN, as a
                non-interactive shell does for a job started with ``&``.
                The setting survives exec.
        """
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self._changed = threading.Condition()
        argv = [sys.executable, "-m", module]
        if sigint_ignored:
            argv = [sys.executable, "-c", _EXEC_WITH_SIGINT_IGNORED, *argv]
        self.process = subprocess.Popen(
            argv,
            cwd=PROJECT_DIR,
            env=child_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._readers = [
            threading.Thread(
                target=self._pump, args=(self.process.stdout, self.stdout)
            ),
            threading.Thread(
                target=self._pump, args=(self.process.stderr, self.stderr)
            ),
        ]
        for reader in self._readers:
            reader.start()

    def _pump(self, stream: IO[str] | None, sink: list[str]) -> None:
        if stream is None:
            return
        for line in stream:
            with self._changed:
                sink.append(line.rstrip("\n"))
                self._changed.notify_all()
        with self._changed:
            self._changed.notify_all()

    def wait_until(
        self, condition: Callable[[], bool], timeout: float = TIMEOUT_S
    ) -> bool:
        """Blocks until ``condition`` holds or the timeout passes."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while not condition():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(min(remaining, 0.1))
        return True

    def wait_for_log(self, text: str, timeout: float = TIMEOUT_S) -> bool:
        """Blocks until a stderr line contains ``text``."""
        return self.wait_until(
            lambda: any(text in line for line in self.stderr), timeout
        )

    def wait_for_results(self, count: int, timeout: float = TIMEOUT_S) -> bool:
        """Blocks until stdout has at least ``count`` lines."""
        return self.wait_until(lambda: len(self.stdout) >= count, timeout)

    def signal(self, sig: signal.Signals) -> None:
        """Sends a signal to the child."""
        self.process.send_signal(sig)

    def finish(self, timeout: float = TIMEOUT_S) -> int:
        """Waits for exit and for all output; kills the child on timeout."""
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise
        finally:
            for reader in self._readers:
                reader.join(timeout)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
        return code

    def kill(self) -> None:
        """Ends the child at once if it is still running."""
        if self.process.poll() is None:
            self.process.kill()
        self.finish()

    def logs(self) -> str:
        """The child's stderr, for assertion messages."""
        return "\n".join(self.stderr)
