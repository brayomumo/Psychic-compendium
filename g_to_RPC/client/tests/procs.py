"""Test helper: runs the real Go productserver on a free port."""

import pathlib
import signal
import subprocess
from types import TracebackType
from typing import Self

from productclient.launch import READY_TIMEOUT_S, stop, wait_until_ready

BINARY = (
    pathlib.Path(__file__).resolve().parents[2]
    / "server"
    / "bin"
    / "productserver"
)
SKIP_REASON = f"server binary not built at {BINARY}; run `make build`"


class RunningServer:
    """Context manager that starts productserver and stops it with SIGTERM.

    Attributes:
        address: ``host:port`` the server listens on, once started.
        exit_code: The server's exit code, once stopped.
    """

    def __init__(self, *extra_args: str) -> None:
        self._args = [str(BINARY), "-addr", "127.0.0.1:0", *extra_args]
        self._proc: subprocess.Popen[str] | None = None
        self.address = ""
        self.exit_code: int | None = None

    def __enter__(self) -> Self:
        # Started directly, not through a shell `&` (which would start it
        # with SIGINT ignored); stderr is discarded to keep test output quiet.
        self._proc = subprocess.Popen(
            self._args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            self.address = wait_until_ready(self._proc, READY_TIMEOUT_S)
        except BaseException:
            self._proc.kill()
            self._proc.wait()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc is None:
            return
        self.exit_code = stop(self._proc)
        if self._proc.stdout is not None:
            self._proc.stdout.close()


STOPPED_BY_SIGTERM = 128 + signal.SIGTERM
