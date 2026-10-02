"""Consumes with manual acknowledgements: ``python -m pubsub.consumer``.

Every delivery ends in exactly one of four ways:

========================  ==================================================
Outcome                   When
========================  ==================================================
ack                       processed, or a duplicate of a processed message
nack, requeue=False       malformed: dead-lettered now, retrying cannot help
nack, requeue=True        the handler raised: retried until the queue's
                          delivery limit dead-letters it
nack, requeue=True        shutdown began before processing started
========================  ==================================================

A message is acknowledged only after it has been processed, so a crash at any
point leaves it unacknowledged, and the broker delivers it again.
"""

import dataclasses
import functools
import logging
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO

import pika.exceptions as amqp

from pubsub import broker, cli
from pubsub.backoff import Backoff
from pubsub.config import Settings
from pubsub.dedupe import RecentIds
from pubsub.messages import InvalidMessageError, Message, decode
from pubsub.shutdown import Shutdown

LOG = logging.getLogger("pubsub.consumer")  # __name__ is __main__ under -m

# Upper bound on how long a stop request waits for the I/O loop to notice.
POLL_S = 0.25
DEDUPE_CAPACITY = 10_000

Handler = Callable[[Message], None]


@dataclasses.dataclass(slots=True)
class ConsumeReport:
    """What a consume run did.

    Attributes:
        processed: messages handled and acknowledged, each counted once.
        duplicates: deliveries acknowledged without processing because the
            message had already been processed.
        dead_lettered: malformed deliveries sent to the dead-letter queue.
        retried: deliveries requeued because the handler raised.
        redelivered: deliveries the broker flagged as redelivered.
    """

    processed: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    retried: int = 0
    redelivered: int = 0


class Consumer:
    """Consumes from the main queue, reconnecting whenever the link drops."""

    def __init__(
        self,
        settings: Settings,
        shutdown: Shutdown,
        handler: Handler,
        *,
        open_session: broker.SessionFactory | None = None,
        out: TextIO | None = None,
    ) -> None:
        """Creates a consumer.

        Args:
            settings: validated settings.
            shutdown: stop flag; the consumer also sets it on reaching
                ``settings.max_messages``.
            handler: processes one message; raising means "retry later".
            open_session: connects and declares the topology. Defaults to
                ``broker.open_session``; tests pass a fake.
            out: where processed ids are written; defaults to stdout.
        """
        self._settings = settings
        self._shutdown = shutdown
        self._handler = handler
        self._backoff = Backoff(
            settings.reconnect_base_s, settings.reconnect_cap_s
        )
        self._open_session = open_session or functools.partial(
            broker.open_session, settings, "consumer", shutdown, self._backoff
        )
        self._out = out or sys.stdout
        self._seen = RecentIds(DEDUPE_CAPACITY)
        self._cancelled_by_broker = False
        self.report = ConsumeReport()

    def run(self) -> ConsumeReport:
        """Consumes until a stop is requested.

        Returns:
            Counts of what happened to each delivery.

        Raises:
            pika.exceptions.AMQPError: on a failure retrying cannot fix.
        """
        while not self._shutdown.requested:
            session = self._open_session()
            if session is None:
                break
            try:
                self._consume(session)
            except amqp.AMQPError as exc:
                # pika wraps socket failures in its own errors. Anything else,
                # such as stdout closing under `| head`, is not an outage and
                # must not be retried as one.
                if not broker.is_transient(exc):
                    raise
                LOG.warning("connection lost (%s)", broker.describe(exc))
            finally:
                session.close()
            if self._cancelled_by_broker and not self._shutdown.requested:
                LOG.warning("broker cancelled the consumer")
            if not self._shutdown.requested:
                # Pause even when connecting works at once: a broker that
                # accepts connections and then drops them must not turn this
                # loop into a hot spin. Processing a message resets the delay.
                delay = self._backoff.next_delay()
                LOG.info("reconnecting in %.2fs", delay)
                self._shutdown.sleep(delay)
        return self.report

    def _consume(self, session: broker.Session) -> None:
        channel = session.channel
        channel.basic_qos(prefetch_count=self._settings.prefetch)
        self._cancelled_by_broker = False
        channel.add_on_cancel_callback(self._on_broker_cancel)
        tag = channel.basic_consume(
            self._settings.queue, self._on_message, auto_ack=False
        )
        LOG.info(
            "consuming from %s (prefetch %d)",
            self._settings.queue,
            self._settings.prefetch,
        )
        # Our own loop instead of start_consuming(): the signal handler only
        # sets a flag, and this loop checks it every POLL_S, so a stop request
        # is honoured without ever calling into pika from a signal handler.
        while not self._shutdown.requested and not self._cancelled_by_broker:
            session.connection.process_data_events(time_limit=POLL_S)
        if self._shutdown.requested:
            # pika requeues deliveries that arrived but were not dispatched.
            channel.basic_cancel(tag)
            LOG.info("stopped consuming")

    def _on_broker_cancel(self, _frame: Any) -> None:
        # The broker cancels consumers when their queue is deleted or its
        # leader moves. Reconnecting declares the queue and consumes again.
        self._cancelled_by_broker = True

    def _on_message(
        self,
        channel: broker.Channel,
        method: Any,
        properties: Any,
        body: bytes,
    ) -> None:
        """Decides the fate of one delivery. Called by pika."""
        tag: int = method.delivery_tag
        if self._shutdown.requested:
            channel.basic_nack(tag, requeue=True)
            return
        if method.redelivered:
            self.report.redelivered += 1

        try:
            message = decode(body, properties)
        except InvalidMessageError as exc:
            LOG.warning("dead-lettering malformed delivery: %s", exc)
            channel.basic_nack(tag, requeue=False)
            self.report.dead_lettered += 1
            return

        if message.message_id in self._seen:
            LOG.info(
                "duplicate %s acknowledged, not reprocessed", message.message_id
            )
            channel.basic_ack(tag)
            self.report.duplicates += 1
            return

        LOG.debug(
            "processing %s (redelivered=%s)",
            message.message_id,
            method.redelivered,
        )
        try:
            self._handler(message)
        except Exception:
            # Any handler failure is retried, because transient faults are
            # common, and the queue's delivery limit dead-letters a message
            # that keeps failing.
            LOG.exception(
                "handler failed on %s; requeueing", message.message_id
            )
            channel.basic_nack(tag, requeue=True)
            self.report.retried += 1
            return

        # Record and report before acking. If the ack is lost with the
        # connection, the redelivery is recognised as a duplicate, and the id
        # still appears on stdout exactly once.
        self._seen.add(message.message_id)
        self.report.processed += 1
        self._out.write(f"{message.message_id}\n")
        channel.basic_ack(tag)
        self._backoff.reset()
        limit = self._settings.max_messages
        if limit and self.report.processed >= limit:
            LOG.info("processed %d messages; stopping", limit)
            self._shutdown.request()


def simulated_work(work_ms: int) -> Handler:
    """Returns a handler that takes ``work_ms`` per message.

    The sleep is not cut short by a shutdown request: a message being
    processed is finished, then acknowledged, before the consumer stops.
    """

    def handle(_message: Message) -> None:
        if work_ms:
            time.sleep(work_ms / 1000)

    return handle


def main(argv: Sequence[str] | None = None) -> int:
    """Consumes until signalled or PUBSUB_MAX_MESSAGES are processed.

    Returns:
        The process exit code.
    """
    cli.parse_args(
        "pubsub.consumer",
        "Consume events with manual acks. Prints one line per processed"
        " message id on stdout; logs go to stderr.",
        argv,
    )
    settings = cli.load_settings("pubsub.consumer")
    if settings is None:
        return cli.EXIT_USAGE
    cli.configure_logging(settings.log_level)
    cli.line_buffered_stdout()
    shutdown = Shutdown()
    shutdown.install()

    consumer = Consumer(settings, shutdown, simulated_work(settings.work_ms))
    try:
        report = consumer.run()
    except (amqp.AMQPError, OSError) as exc:
        LOG.error("giving up: %s", broker.describe(exc))
        return cli.EXIT_FAILURE
    LOG.info(
        "processed %d, duplicates %d, dead-lettered %d, retried %d,"
        " redelivered %d",
        report.processed,
        report.duplicates,
        report.dead_lettered,
        report.retried,
        report.redelivered,
    )
    limit_reached = bool(settings.max_messages) and (
        report.processed >= settings.max_messages
    )
    return cli.exit_code(shutdown, interrupted=not limit_reached)


if __name__ == "__main__":
    sys.exit(main())
