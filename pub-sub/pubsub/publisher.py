"""Publishes messages with publisher confirms: ``python -m pubsub.publisher``.

The guarantee: a message id is printed to stdout only after the broker has
confirmed the message. A confirm means the broker routed the message to the
durable queue and stored it; a quorum queue confirms after the write is
committed. The message is published with ``mandatory=True``, so if no queue
is bound, the broker returns it and pika raises instead of the message being
dropped without a word.

If the connection drops before the confirm arrives, nobody knows whether the
broker stored the message. The publisher reconnects and publishes it again
with the same ``message_id``. That makes delivery at-least-once, and the
consumer deduplicates.
"""

import dataclasses
import functools
import logging
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import TextIO

import pika.exceptions as amqp

from pubsub import broker, cli
from pubsub.backoff import Backoff
from pubsub.config import Settings
from pubsub.messages import USER_CREATED, InvalidMessageError, Message, encode
from pubsub.shutdown import Shutdown

LOG = logging.getLogger("pubsub.publisher")  # __name__ is __main__ under -m

# Consecutive refusals (nack or unroutable) of one message before giving up.
# A connection loss is not a refusal and is retried for as long as it takes.
MAX_REFUSALS = 5
# Longest stretch without servicing the connection while pausing between
# publishes; well under any heartbeat timeout.
_IDLE_SLICE_S = 0.25


class PublishError(RuntimeError):
    """The broker keeps refusing a message; retrying will not help."""


@dataclasses.dataclass(slots=True)
class PublishReport:
    """What a publish run achieved.

    Attributes:
        confirmed: messages the broker confirmed.
        republished: publishes repeated after a connection loss left the
            outcome unknown. Each may have produced a duplicate.
        interrupted: True if a stop request ended the run early.
    """

    confirmed: int = 0
    republished: int = 0
    interrupted: bool = False


class Publisher:
    """Publishes messages one at a time, waiting for each confirm."""

    def __init__(
        self,
        settings: Settings,
        shutdown: Shutdown,
        *,
        open_session: broker.SessionFactory | None = None,
        out: TextIO | None = None,
    ) -> None:
        """Creates a publisher. It connects lazily, on the first publish.

        Args:
            settings: validated settings.
            shutdown: stop flag, checked between messages.
            open_session: connects and declares the topology. Defaults to
                ``broker.open_session``; tests pass a fake.
            out: where confirmed ids are written; defaults to stdout.
        """
        self._settings = settings
        self._shutdown = shutdown
        self._backoff = Backoff(
            settings.reconnect_base_s, settings.reconnect_cap_s
        )
        self._open_session = open_session or functools.partial(
            broker.open_session,
            settings,
            "publisher",
            shutdown,
            self._backoff,
            confirms=True,
        )
        self._out = out or sys.stdout
        self._session: broker.Session | None = None

    def publish_all(self, messages: Iterable[Message]) -> PublishReport:
        """Publishes every message, or stops early when asked to.

        Args:
            messages: the messages, in order.

        Returns:
            Counts of what was confirmed and repeated.

        Raises:
            PublishError: if the broker keeps refusing a message.
            pika.exceptions.AMQPError: on a failure retrying cannot fix.
        """
        report = PublishReport()
        interval_s = self._settings.publish_interval_ms / 1000
        try:
            for index, message in enumerate(messages):
                if index and interval_s and not self._idle(interval_s):
                    report.interrupted = True
                    break
                if not self._publish_one(message, report):
                    report.interrupted = True
                    break
        finally:
            self._drop_session()
        return report

    def _publish_one(self, message: Message, report: PublishReport) -> bool:
        """Publishes until confirmed. Returns False if stopped first."""
        body, properties = encode(message)
        refusals = 0
        while not self._shutdown.requested:
            session = self._session or self._connect()
            if session is None:
                return False
            try:
                session.channel.basic_publish(
                    exchange=self._settings.exchange,
                    routing_key=self._settings.routing_key,
                    body=body,
                    properties=properties,
                    mandatory=True,
                )
            except amqp.UnroutableError:
                # No queue is bound, perhaps deleted by an operator.
                # Reconnecting re-declares the topology.
                refusals += 1
                LOG.warning("%s was unroutable", message.message_id)
                self._drop_session()
            except amqp.NackError:
                refusals += 1
                delay = self._backoff.next_delay()
                LOG.warning(
                    "broker nacked %s; retrying in %.2fs",
                    message.message_id,
                    delay,
                )
                self._shutdown.sleep(delay)
            except amqp.AMQPError as exc:
                # Only pika's errors: a failing stdout is not a broker outage.
                if not broker.is_transient(exc):
                    raise
                report.republished += 1
                LOG.warning(
                    "connection lost before %s was confirmed (%s);"
                    " publishing it again, so the consumer may see it twice",
                    message.message_id,
                    broker.describe(exc),
                )
                self._drop_session()
            else:
                self._backoff.reset()
                report.confirmed += 1
                # The id as it went on the wire: encode() canonicalises it.
                self._out.write(f"{properties.message_id}\n")
                return True
            if refusals >= MAX_REFUSALS:
                raise PublishError(
                    f"broker refused {message.message_id} {refusals} times"
                )
        return False

    def _idle(self, seconds: float) -> bool:
        """Pauses while servicing heartbeats. Returns False if stopped."""
        deadline = time.monotonic() + seconds
        while not self._shutdown.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._session is None:
                return self._shutdown.sleep(remaining)
            try:
                self._session.connection.process_data_events(
                    time_limit=min(remaining, _IDLE_SLICE_S)
                )
            except amqp.AMQPError as exc:
                if not broker.is_transient(exc):
                    raise
                LOG.warning(
                    "connection lost while idle (%s)", broker.describe(exc)
                )
                self._drop_session()
        return False

    def _connect(self) -> broker.Session | None:
        self._session = self._open_session()
        return self._session

    def _drop_session(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


def demo_messages(count: int) -> Iterator[Message]:
    """Yields ``count`` sample user.created events."""
    for number in range(count):
        yield Message.create(
            USER_CREATED,
            {"user_id": number, "email": f"user{number}@example.com"},
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Publishes PUBSUB_MESSAGE_COUNT messages and prints each confirmed id.

    Returns:
        The process exit code.
    """
    cli.parse_args(
        "pubsub.publisher",
        "Publish sample events with publisher confirms. Prints one line per"
        " confirmed message id on stdout; logs go to stderr.",
        argv,
    )
    settings = cli.load_settings("pubsub.publisher")
    if settings is None:
        return cli.EXIT_USAGE
    cli.configure_logging(settings.log_level)
    cli.line_buffered_stdout()
    shutdown = Shutdown()
    shutdown.install()

    publisher = Publisher(settings, shutdown)
    try:
        report = publisher.publish_all(demo_messages(settings.message_count))
    except (PublishError, InvalidMessageError) as exc:
        LOG.error("%s", exc)
        return cli.EXIT_FAILURE
    except (amqp.AMQPError, OSError) as exc:
        LOG.error("giving up: %s", broker.describe(exc))
        return cli.EXIT_FAILURE
    LOG.info(
        "confirmed %d of %d messages; %d republished after a lost connection%s",
        report.confirmed,
        settings.message_count,
        report.republished,
        " (stopped early)" if report.interrupted else "",
    )
    return cli.exit_code(shutdown, interrupted=report.interrupted)


if __name__ == "__main__":
    sys.exit(main())
