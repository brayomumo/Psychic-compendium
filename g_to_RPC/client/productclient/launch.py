"""Starts the Go server, runs the demo against it, and stops it cleanly.

This is what ``make run`` does. The server is started directly (never via
a shell ``&``, which would start it with SIGINT ignored), its readiness line
gives the real address, and it is always stopped and reaped, even if the
demo fails or the user presses Ctrl+C.

Usage: python -m productclient.launch --server PATH [--port N]
"""

import argparse
import logging
import signal
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

from productclient import demo

__all__ = ["main"]

logger = logging.getLogger("productclient.launch")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130
READY_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 15.0
# 128 + SIGTERM: the server's exit code after a clean, signal-driven stop.
SERVER_STOPPED_CLEANLY = 128 + signal.SIGTERM
READY_PREFIX = "listening on "
MAX_PORT = 65535


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if not 0 <= value <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"must be in 0..{MAX_PORT}, got {text}"
        )
    return value


def wait_until_ready(server: subprocess.Popen[str], timeout: float) -> str:
    """Reads the server's readiness line and returns the address it gives.

    Args:
        server: The started server, with stdout piped as text.
        timeout: Seconds to wait before giving up.

    Returns:
        The ``host:port`` the server is listening on.

    Raises:
        RuntimeError: The server exited or stayed silent past the timeout.
    """
    if server.stdout is None:
        raise RuntimeError("server stdout is not piped")
    stdout = server.stdout
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: lines.append(stdout.readline()), daemon=True
    )
    reader.start()
    reader.join(timeout)
    line = lines[0].strip() if lines else ""
    if not line.startswith(READY_PREFIX):
        raise RuntimeError(
            f"server not ready after {timeout:g}s (first line: {line!r})"
        )
    return line.removeprefix(READY_PREFIX)


def stop(server: subprocess.Popen[str]) -> int:
    """Asks the server to stop with SIGTERM, killing it if it will not.

    Args:
        server: The running server.

    Returns:
        The server's exit code.
    """
    if server.poll() is None:
        server.send_signal(signal.SIGTERM)
    try:
        return server.wait(STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning(
            "server ignored SIGTERM for %gs; killing it", STOP_TIMEOUT_S
        )
        server.kill()
        return server.wait()


def main(argv: Sequence[str] | None = None) -> int:
    """Runs server plus demo.

    Args:
        argv: Command-line arguments; None means sys.argv[1:].

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--server", type=Path, required=True, help="path to productserver"
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=50059,
        help="port to serve on; 0 picks a free one (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.server.is_file():
        logger.error("no server binary at %s; run `make build`", args.server)
        return EXIT_FAILURE

    server = subprocess.Popen(
        [str(args.server), "-addr", f"127.0.0.1:{args.port}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            addr = wait_until_ready(server, READY_TIMEOUT_S)
        except RuntimeError as e:
            logger.error("%s", e)
            return EXIT_FAILURE
        logger.info("server ready on %s", addr)
        status = demo.main(["--target", addr])
    except KeyboardInterrupt:
        logger.warning("interrupted; stopping the server")
        return EXIT_INTERRUPTED
    finally:
        code = stop(server)
        if server.stdout is not None:
            server.stdout.close()
        logger.info("server exited with %d", code)
    if code != SERVER_STOPPED_CLEANLY:
        logger.error(
            "server exit code %d, want %d", code, SERVER_STOPPED_CLEANLY
        )
        return EXIT_FAILURE
    return status


if __name__ == "__main__":
    raise SystemExit(main())
