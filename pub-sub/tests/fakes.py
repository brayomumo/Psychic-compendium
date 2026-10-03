"""In-memory stand-ins for pika objects, for tests that need no broker."""

import dataclasses
from collections.abc import Callable, Iterator
from typing import Any

from pubsub import broker


@dataclasses.dataclass
class Method:
    """The fields of pika's Basic.Deliver that the consumer reads."""

    delivery_tag: int = 1
    redelivered: bool = False


@dataclasses.dataclass
class Props:
    """The fields of pika.BasicProperties that ``decode`` reads."""

    content_type: str | None = "application/json"
    message_id: str | None = None


class FakeConnection:
    """Records process_data_events calls; can run a hook on each one."""

    def __init__(self, on_events: Callable[[], None] | None = None) -> None:
        self.is_open = True
        self.closed = False
        self.event_calls = 0
        self._on_events = on_events

    def process_data_events(self, time_limit: float | None = 0) -> None:
        del time_limit
        self.event_calls += 1
        if self._on_events is not None:
            self._on_events()

    def close(self) -> None:
        self.closed = True
        self.is_open = False


class FakeChannel:
    """Records acks, nacks and publishes; raises scripted publish errors."""

    def __init__(
        self, publish_errors: Iterator[Exception] | None = None
    ) -> None:
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []
        self.published: list[tuple[str, str, bytes, Any, bool]] = []
        self.cancelled: list[str] = []
        self.prefetch: int | None = None
        self.on_cancel: Callable[[Any], None] | None = None
        self.on_message: Callable[..., None] | None = None
        self._publish_errors = publish_errors

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: Any = None,
        mandatory: bool = False,
    ) -> None:
        if self._publish_errors is not None:
            error = next(self._publish_errors, None)
            if error is not None:
                raise error
        self.published.append(
            (exchange, routing_key, body, properties, mandatory)
        )

    def basic_ack(self, delivery_tag: int = 0, multiple: bool = False) -> None:
        del multiple
        self.acks.append(delivery_tag)

    def basic_nack(
        self,
        delivery_tag: int = 0,
        multiple: bool = False,
        requeue: bool = True,
    ) -> None:
        del multiple
        self.nacks.append((delivery_tag, requeue))

    def basic_qos(self, prefetch_count: int = 0) -> None:
        self.prefetch = prefetch_count

    def basic_consume(
        self,
        queue: str,
        on_message_callback: Callable[..., None],
        auto_ack: bool = False,
    ) -> str:
        del queue
        assert not auto_ack, "the consumer must use manual acks"
        self.on_message = on_message_callback
        return "ctag-1"

    def basic_cancel(self, consumer_tag: str) -> object:
        self.cancelled.append(consumer_tag)
        return []

    def add_on_cancel_callback(self, callback: Callable[[Any], None]) -> None:
        self.on_cancel = callback


class SessionScript:
    """A session factory that hands out prepared sessions in order.

    Once the script runs out it returns None, which the publisher and the
    consumer treat as "stop requested while connecting".
    """

    def __init__(self, *sessions: broker.Session) -> None:
        self._sessions = list(sessions)
        self.opened = 0

    def __call__(self) -> broker.Session | None:
        if not self._sessions:
            return None
        self.opened += 1
        return self._sessions.pop(0)


def session(
    channel: FakeChannel | None = None,
    connection: FakeConnection | None = None,
) -> broker.Session:
    """Builds a session from fakes."""
    return broker.Session(
        connection or FakeConnection(), channel or FakeChannel()
    )
