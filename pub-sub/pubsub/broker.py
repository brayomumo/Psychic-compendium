"""Connections, topology and error classification shared by both sides.

pika's BlockingConnection is not thread-safe. Each process here owns exactly
one connection and one channel, and uses them from its main thread only.
"""

import dataclasses
import logging
import os
import re
from collections.abc import Callable
from typing import Any, Protocol

import pika
import pika.exceptions as amqp
from pika.exchange_type import ExchangeType

from pubsub.backoff import Backoff
from pubsub.config import Settings
from pubsub.shutdown import Shutdown

LOG = logging.getLogger(__name__)

# Fail, instead of hanging forever, when the broker stops reading from this
# connection because of a memory or disk alarm.
BLOCKED_CONNECTION_TIMEOUT_S = 60.0
SOCKET_TIMEOUT_S = 5.0

# pika names a failed handshake after the stage at which the socket died, not
# after the cause. A broker that drops the socket while shutting down therefore
# looks like an authentication or protocol error (observed during a restart in
# test_integration). Only an explicit refusal from the broker is permanent:
# 403 ACCESS_REFUSED for bad credentials, 530 NOT_ALLOWED for a missing or
# forbidden vhost. pika keeps that reply code only in the message text.
_HANDSHAKE_ERRORS = (
    amqp.ProbableAuthenticationError,
    amqp.ProbableAccessDeniedError,
    amqp.IncompatibleProtocolError,
)
_BROKER_REFUSED = re.compile(r"ConnectionClosedByBroker: \((403|530)\)")
# Channel-level closes that happen while a broker restarts or a quorum queue
# elects a leader: 320 CONNECTION_FORCED, 506 RESOURCE_ERROR, 541
# INTERNAL_ERROR. Anything else (403, 404, 406 PRECONDITION_FAILED) is a bug
# or a topology mismatch, and retrying cannot fix it.
_TRANSIENT_CHANNEL_CODES = frozenset({320, 506, 541})
_PRECONDITION_FAILED = 406


class Connection(Protocol):
    """The subset of pika.BlockingConnection this package uses."""

    @property
    def is_open(self) -> bool:
        """True while the connection is usable."""

    def process_data_events(self, time_limit: float | None = 0) -> None:
        """Runs the I/O loop: heartbeats, deliveries and callbacks."""

    def close(self) -> None:
        """Closes the connection and its channels."""


class Channel(Protocol):
    """The subset of pika's BlockingChannel this package uses."""

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: Any = None,
        mandatory: bool = False,
    ) -> None:
        """Publishes; with confirms on, returns once the broker confirms."""

    def basic_ack(self, delivery_tag: int = 0, multiple: bool = False) -> None:
        """Acknowledges a delivery."""

    def basic_nack(
        self,
        delivery_tag: int = 0,
        multiple: bool = False,
        requeue: bool = True,
    ) -> None:
        """Rejects a delivery, requeueing it or dead-lettering it."""

    def basic_qos(self, prefetch_count: int = 0) -> None:
        """Caps unacknowledged deliveries in flight to this channel."""

    def basic_consume(
        self,
        queue: str,
        on_message_callback: Callable[..., None],
        auto_ack: bool = False,
    ) -> str:
        """Starts a consumer and returns its tag."""

    def basic_cancel(self, consumer_tag: str) -> object:
        """Stops a consumer; pika requeues deliveries not yet dispatched."""

    def add_on_cancel_callback(self, callback: Callable[[Any], None]) -> None:
        """Registers a callback for a broker-initiated consumer cancel."""


@dataclasses.dataclass(frozen=True, slots=True)
class Session:
    """One connection and its single channel, owned by one thread.

    Attributes:
        connection: the open connection.
        channel: a channel on it with the topology already declared.
    """

    connection: Connection
    channel: Channel

    def close(self) -> None:
        """Closes the connection, ignoring errors from one already dead."""
        close_quietly(self.connection)


SessionFactory = Callable[[], Session | None]


def is_transient(exc: BaseException) -> bool:
    """Tells whether reconnecting and retrying may succeed.

    Args:
        exc: an exception raised by pika or the socket layer.

    Returns:
        True for lost connections and broker restarts, False for errors
        that will recur, such as bad credentials or a topology mismatch.
        Retrying a permanent error would only bury a misconfiguration
        under an endless stream of reconnect warnings.
    """
    if isinstance(exc, amqp.AuthenticationError):
        return False  # No SASL mechanism in common with the broker.
    if isinstance(exc, _HANDSHAKE_ERRORS):
        return _BROKER_REFUSED.search(str(exc)) is None
    if isinstance(exc, amqp.ChannelClosedByBroker):
        return exc.reply_code in _TRANSIENT_CHANNEL_CODES
    return isinstance(
        exc,
        amqp.AMQPConnectionError | amqp.ChannelWrongStateError | OSError,
    )


def describe(exc: BaseException) -> str:
    """Formats an exception for a one-line log message."""
    detail = str(exc) or repr(exc.args)
    if (
        isinstance(exc, amqp.ChannelClosedByBroker)
        and exc.reply_code == _PRECONDITION_FAILED
    ):
        detail += (
            " (a queue or exchange already exists with different settings;"
            " delete it, or manage such settings through a policy)"
        )
    return f"{type(exc).__name__}: {detail}"


def connection_parameters(settings: Settings, role: str) -> Any:
    """Builds pika connection parameters for one process.

    Args:
        settings: validated settings.
        role: "publisher" or "consumer"; names the connection in the
            management UI so an operator can tell clients apart.

    Returns:
        A ``pika.URLParameters`` instance.
    """
    params = pika.URLParameters(settings.url)
    params.heartbeat = settings.heartbeat_s
    params.blocked_connection_timeout = BLOCKED_CONNECTION_TIMEOUT_S
    params.socket_timeout = SOCKET_TIMEOUT_S
    # Retries are ours: jittered, logged, and interruptible by a signal.
    params.connection_attempts = 1
    params.client_properties = {
        "connection_name": f"pubsub-{role}-{os.getpid()}"
    }
    return params


def queue_arguments(settings: Settings) -> dict[str, object]:
    """Returns the x-arguments for the main queue.

    A quorum queue is durable and replicated, and it counts deliveries, which
    lets it dead-letter a message that keeps failing instead of looping on
    it forever. At-least-once dead-lettering keeps a rejected message in the
    source queue until the dead-letter queue has confirmed it; RabbitMQ
    requires ``reject-publish`` overflow for that mode.
    """
    return {
        "x-queue-type": "quorum",
        "x-delivery-limit": settings.delivery_limit,
        "x-dead-letter-exchange": settings.dead_letter_exchange,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }


def declare_topology(channel: Any, settings: Settings) -> None:
    """Declares exchanges, queues and bindings.

    Declarations are idempotent. Both sides run this on every connect, so
    whichever starts first creates the topology, and a publisher that starts
    before any consumer still has a queue to route into. Re-declaring with
    different arguments fails with 406 PRECONDITION_FAILED.

    Args:
        channel: an open pika channel.
        settings: names and limits.
    """
    channel.exchange_declare(
        settings.dead_letter_exchange,
        exchange_type=ExchangeType.fanout,
        durable=True,
    )
    channel.queue_declare(
        settings.dead_letter_queue,
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    channel.queue_bind(
        settings.dead_letter_queue, settings.dead_letter_exchange
    )
    channel.exchange_declare(
        settings.exchange, exchange_type=ExchangeType.direct, durable=True
    )
    channel.queue_declare(
        settings.queue, durable=True, arguments=queue_arguments(settings)
    )
    channel.queue_bind(
        settings.queue, settings.exchange, routing_key=settings.routing_key
    )


def open_session(
    settings: Settings,
    role: str,
    shutdown: Shutdown,
    backoff: Backoff,
    *,
    confirms: bool = False,
) -> Session | None:
    """Connects and declares the topology, retrying transient failures.

    Args:
        settings: validated settings.
        role: "publisher" or "consumer", for logs and the connection name.
        shutdown: checked between attempts, and cuts backoff sleeps short.
        backoff: supplies the delay between attempts. The caller resets it
            once the session has done useful work.
        confirms: put the channel in publisher-confirm mode.

    Returns:
        A ready session, or None if a stop was requested first.

    Raises:
        pika.exceptions.AMQPError: on a failure that retrying cannot fix.
    """
    while not shutdown.requested:
        connection = None
        try:
            connection = pika.BlockingConnection(
                connection_parameters(settings, role)
            )
            channel = connection.channel()
            if confirms:
                channel.confirm_delivery()
            declare_topology(channel, settings)
        except (amqp.AMQPError, OSError) as exc:
            close_quietly(connection)
            if not is_transient(exc):
                raise
            delay = backoff.next_delay()
            LOG.warning(
                "%s: cannot reach broker at %s (%s); retry %d in %.2fs",
                role,
                settings.redacted_url(),
                describe(exc),
                backoff.attempt,
                delay,
            )
            shutdown.sleep(delay)
            continue
        LOG.info("%s: connected to %s", role, settings.redacted_url())
        return Session(connection, channel)
    return None


def close_quietly(connection: Connection | None) -> None:
    """Closes a connection, ignoring errors from one that is already dead."""
    if connection is None or not connection.is_open:
        return
    try:
        connection.close()
    except (amqp.AMQPError, OSError) as exc:
        LOG.debug("ignoring error while closing connection: %s", describe(exc))
