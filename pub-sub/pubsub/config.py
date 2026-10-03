"""Settings loaded from environment variables and validated up front.

Every problem is collected and reported together, so a misconfigured process
fails once, at startup, with a complete list instead of one error per restart.
An empty variable counts as unset.
"""

import dataclasses
from collections.abc import Callable, Mapping
from typing import Self, TypeVar
from urllib.parse import urlsplit

DEFAULT_URL = "amqp://guest:guest@localhost:5679/%2F"

# AMQP 0-9-1 encodes names as short strings: at most 255 bytes.
_MAX_NAME_BYTES = 255
# The broker refuses to let clients declare names with this prefix.
_RESERVED_PREFIX = "amq."
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_T = TypeVar("_T", int, float)


class ConfigError(ValueError):
    """One or more settings are invalid. The message lists every problem."""


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings shared by the publisher and the consumer.

    Attributes:
        url: AMQP URL of the broker (RABBITMQ_URL).
        exchange: durable direct exchange messages are published to.
        queue: durable quorum queue the consumer reads from.
        routing_key: binding key between the exchange and the queue.
        dead_letter_exchange: fanout exchange for rejected messages.
        dead_letter_queue: queue that keeps rejected messages for inspection.
        delivery_limit: deliveries a message may fail before it is dead-
            lettered. Every return counts, including those caused by a
            consumer disconnecting with the message unacknowledged.
        prefetch: unacknowledged messages the broker may push to one consumer.
        message_count: messages the publisher sends before exiting.
        max_messages: consumer exits after processing this many messages;
            0 means run until signalled.
        publish_interval_ms: pause between publishes, to simulate a trickle.
        work_ms: simulated processing time per message in the consumer.
        heartbeat_s: AMQP heartbeat timeout negotiated with the broker.
        reconnect_base_s: first reconnect backoff ceiling.
        reconnect_cap_s: largest reconnect backoff ceiling.
        log_level: logging level name for the entry points.
    """

    url: str = DEFAULT_URL
    exchange: str = "pubsub.events"
    queue: str = "pubsub.events.worker"
    routing_key: str = "user.created"
    dead_letter_exchange: str = "pubsub.events.dlx"
    dead_letter_queue: str = "pubsub.events.worker.dlq"
    delivery_limit: int = 20
    prefetch: int = 10
    message_count: int = 100
    max_messages: int = 0
    publish_interval_ms: int = 0
    work_ms: int = 0
    heartbeat_s: int = 30
    reconnect_base_s: float = 0.5
    reconnect_cap_s: float = 15.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """Builds settings from environment variables.

        Args:
            env: the environment, usually ``os.environ``.

        Returns:
            Validated settings; unset variables take the defaults.

        Raises:
            ConfigError: if any variable is invalid. Lists all problems.
        """
        reader = _Reader(env)
        d = cls()
        settings = cls(
            url=reader.text("RABBITMQ_URL", d.url),
            exchange=reader.text("PUBSUB_EXCHANGE", d.exchange),
            queue=reader.text("PUBSUB_QUEUE", d.queue),
            routing_key=reader.text("PUBSUB_ROUTING_KEY", d.routing_key),
            dead_letter_exchange=reader.text(
                "PUBSUB_DLX", d.dead_letter_exchange
            ),
            dead_letter_queue=reader.text("PUBSUB_DLQ", d.dead_letter_queue),
            delivery_limit=reader.number(
                "PUBSUB_DELIVERY_LIMIT", d.delivery_limit, int, 1, 1000
            ),
            prefetch=reader.number(
                "PUBSUB_PREFETCH", d.prefetch, int, 1, 65535
            ),
            message_count=reader.number(
                "PUBSUB_MESSAGE_COUNT", d.message_count, int, 0, 10_000_000
            ),
            max_messages=reader.number(
                "PUBSUB_MAX_MESSAGES", d.max_messages, int, 0, 10_000_000
            ),
            publish_interval_ms=reader.number(
                "PUBSUB_PUBLISH_INTERVAL_MS",
                d.publish_interval_ms,
                int,
                0,
                60_000,
            ),
            work_ms=reader.number("PUBSUB_WORK_MS", d.work_ms, int, 0, 60_000),
            heartbeat_s=reader.number(
                "PUBSUB_HEARTBEAT_S", d.heartbeat_s, int, 1, 600
            ),
            reconnect_base_s=reader.number(
                "PUBSUB_RECONNECT_BASE_S",
                d.reconnect_base_s,
                float,
                0.001,
                60.0,
            ),
            reconnect_cap_s=reader.number(
                "PUBSUB_RECONNECT_CAP_S", d.reconnect_cap_s, float, 0.001, 600.0
            ),
            log_level=reader.text("PUBSUB_LOG_LEVEL", d.log_level).upper(),
        )
        reader.errors.extend(settings.problems())
        if reader.errors:
            raise ConfigError(
                "invalid configuration:\n  " + "\n  ".join(reader.errors)
            )
        return settings

    def problems(self) -> list[str]:
        """Returns cross-field and format problems; empty when valid."""
        found: list[str] = []
        parts = urlsplit(self.url)
        try:
            port_ok = parts.port is None or parts.port > 0
        except ValueError:
            port_ok = False
        if (
            parts.scheme not in ("amqp", "amqps")
            or not parts.hostname
            or not port_ok
        ):
            found.append(
                "RABBITMQ_URL must look like amqp://user:pass@host:port/vhost"
            )
        names = {
            "PUBSUB_EXCHANGE": self.exchange,
            "PUBSUB_QUEUE": self.queue,
            "PUBSUB_ROUTING_KEY": self.routing_key,
            "PUBSUB_DLX": self.dead_letter_exchange,
            "PUBSUB_DLQ": self.dead_letter_queue,
        }
        for key, name in names.items():
            if not name.strip():
                found.append(f"{key} must not be blank")
            elif len(name.encode()) > _MAX_NAME_BYTES:
                found.append(f"{key} must be at most {_MAX_NAME_BYTES} bytes")
            elif (
                name.startswith(_RESERVED_PREFIX)
                and key != "PUBSUB_ROUTING_KEY"
            ):
                found.append(
                    f"{key} must not start with reserved '{_RESERVED_PREFIX}'"
                )
        if self.queue == self.dead_letter_queue:
            found.append("PUBSUB_QUEUE and PUBSUB_DLQ must differ")
        if self.exchange == self.dead_letter_exchange:
            found.append("PUBSUB_EXCHANGE and PUBSUB_DLX must differ")
        # pika's blocking adapter only answers heartbeats between callbacks,
        # so a handler that outlives the heartbeat timeout gets its
        # connection closed by the broker, and its message redelivered forever.
        if self.work_ms * 2 >= self.heartbeat_s * 1000:
            found.append(
                "PUBSUB_WORK_MS must be under half of PUBSUB_HEARTBEAT_S,"
                " or the broker drops the connection mid-message"
            )
        if self.reconnect_cap_s < self.reconnect_base_s:
            found.append(
                "PUBSUB_RECONNECT_CAP_S must be >= PUBSUB_RECONNECT_BASE_S"
            )
        if self.log_level not in _LOG_LEVELS:
            found.append(
                f"PUBSUB_LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}"
            )
        return found

    def redacted_url(self) -> str:
        """Returns the broker URL with any password replaced, for logging."""
        parts = urlsplit(self.url)
        if parts.password is None:
            return self.url
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
        return parts._replace(netloc=netloc).geturl()


class _Reader:
    """Reads raw environment values and records problems instead of raising."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def text(self, key: str, default: str) -> str:
        value = self._env.get(key, "")
        return value if value.strip() else default

    def number(
        self,
        key: str,
        default: _T,
        parse: Callable[[str], _T],
        low: _T,
        high: _T,
    ) -> _T:
        raw = self._env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = parse(raw)
        except ValueError:
            self.errors.append(f"{key}={raw!r} is not a valid {parse.__name__}")
            return default
        if not low <= value <= high:
            self.errors.append(f"{key}={raw} must be between {low} and {high}")
        return value
