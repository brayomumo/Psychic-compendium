"""Entry-point plumbing shared by the publisher, the consumer and the demo.

Exit codes follow the repo convention: 0 success, 1 runtime failure, 2 usage
or configuration error, and 128 + signal number (130 for SIGINT, 143 for
SIGTERM) after a clean shutdown that a signal cut short.
"""

import argparse
import io
import logging
import os
import sys
from collections.abc import Sequence

from pubsub.config import ConfigError, Settings
from pubsub.shutdown import Shutdown

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

ENVIRONMENT_HELP = """\
environment variables (an empty value counts as unset):
  RABBITMQ_URL                broker URL
                              (amqp://guest:guest@localhost:5679/%2F)
  PUBSUB_EXCHANGE             main exchange (pubsub.events)
  PUBSUB_QUEUE                main queue (pubsub.events.worker)
  PUBSUB_ROUTING_KEY          binding key (user.created)
  PUBSUB_DLX                  dead-letter exchange (pubsub.events.dlx)
  PUBSUB_DLQ                  dead-letter queue (pubsub.events.worker.dlq)
  PUBSUB_DELIVERY_LIMIT       failed deliveries before dead-lettering (20)
  PUBSUB_PREFETCH             unacked deliveries per consumer (10)
  PUBSUB_MESSAGE_COUNT        messages the publisher sends (100)
  PUBSUB_MAX_MESSAGES         consumer exits after N; 0 = run until
                              signalled (0)
  PUBSUB_PUBLISH_INTERVAL_MS  pause between publishes (0)
  PUBSUB_WORK_MS              simulated work per message (0)
  PUBSUB_HEARTBEAT_S          AMQP heartbeat timeout (30)
  PUBSUB_RECONNECT_BASE_S     first reconnect backoff ceiling (0.5)
  PUBSUB_RECONNECT_CAP_S      largest reconnect backoff ceiling (15)
  PUBSUB_LOG_LEVEL            DEBUG, INFO, WARNING or ERROR (INFO)
"""


def parse_args(prog: str, description: str, argv: Sequence[str] | None) -> None:
    """Handles ``--help`` and rejects unknown arguments with exit code 2.

    Configuration comes from the environment, so there are no options; the
    parser exists so that a typo fails loudly instead of being ignored.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog=ENVIRONMENT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)


def load_settings(prog: str) -> Settings | None:
    """Reads settings from the environment, reporting problems on stderr.

    Returns:
        The settings, or None if they are invalid (exit with EXIT_USAGE).
    """
    try:
        return Settings.from_env(os.environ)
    except ConfigError as exc:
        sys.stderr.write(f"{prog}: {exc}\n")
        return None


def configure_logging(level: str) -> None:
    """Sends diagnostics to stderr; stdout stays reserved for results."""
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # pika logs every dropped socket at ERROR with a traceback. Our code
    # reports connection loss once, with context, so silence pika unless
    # someone is debugging it.
    if level != "DEBUG":
        logging.getLogger("pika").setLevel(logging.CRITICAL)


def line_buffered_stdout() -> None:
    """Flushes each result line immediately.

    Readers of stdout (a pipe, a test, ``tee``) see every id as soon as it
    is confirmed or processed, and a crash loses at most a partial line.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)


def exit_code(shutdown: Shutdown, *, interrupted: bool) -> int:
    """Maps how the run ended to a process exit code.

    Args:
        shutdown: the process's stop flag.
        interrupted: True if the run stopped before finishing its work.

    Returns:
        0 if the work finished, else 128 + the signal that stopped it.
    """
    if interrupted and shutdown.signum is not None:
        return 128 + shutdown.signum
    return EXIT_OK
