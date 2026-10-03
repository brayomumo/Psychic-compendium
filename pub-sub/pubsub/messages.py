"""The message envelope and its AMQP encoding.

The JSON body is the source of truth and describes itself, so a message stays
meaningful after it has been dead-lettered, shovelled or dumped to a file. The
AMQP properties mirror it for the broker and its tooling: the management UI
shows ``message_id`` and ``type``, and ``delivery_mode=2`` (persistent) is what
makes the broker write the message to disk.

``encode`` and ``decode`` apply the same validation, so the publisher cannot
emit a message that the consumer would have to dead-letter.
"""

import dataclasses
import datetime
import json
import uuid
from typing import Any, NoReturn, Protocol, Self

import pika

CONTENT_TYPE = "application/json"
CONTENT_ENCODING = "utf-8"
APP_ID = "psychic-compendium.pubsub"
# Bounds memory per delivery; anything larger is a producer bug, not data.
MAX_BODY_BYTES = 64 * 1024
USER_CREATED = "user.created"
KNOWN_TYPES = frozenset({USER_CREATED})


class InvalidMessageError(ValueError):
    """A delivery that can never be processed; it belongs in the DLQ."""


class Properties(Protocol):
    """The AMQP properties ``decode`` reads (pika.BasicProperties fits)."""

    content_type: str | None
    message_id: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class Message:
    """One event: who it is (id), what it is (type), when, and its data.

    Attributes:
        message_id: canonical lowercase UUID; the deduplication key.
        type: one of ``KNOWN_TYPES``.
        timestamp: timezone-aware creation time.
        payload: JSON object with the event data.
    """

    message_id: str
    type: str
    timestamp: datetime.datetime
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        type_: str,
        payload: dict[str, Any],
        *,
        now: datetime.datetime | None = None,
    ) -> Self:
        """Builds a message with a fresh random id.

        Args:
            type_: the message type.
            payload: the event data; must be JSON-serialisable.
            now: creation time; defaults to the current UTC time.

        Returns:
            The new message.
        """
        timestamp = now or datetime.datetime.now(datetime.UTC)
        return cls(str(uuid.uuid4()), type_, timestamp, payload)


def encode(message: Message) -> tuple[bytes, Any]:
    """Serialises a message into an AMQP body and properties.

    Args:
        message: the message to send.

    Returns:
        The UTF-8 JSON body and the matching ``pika.BasicProperties``.

    Raises:
        InvalidMessageError: if the message would be rejected by ``decode``.
    """
    message_id = _canonical_id(message.message_id)
    _checked_type(message.type)
    _check_timestamp(message.timestamp)
    envelope = {
        "message_id": message_id,
        "type": message.type,
        "timestamp": message.timestamp.isoformat(),
        "payload": message.payload,
    }
    try:
        text = json.dumps(
            envelope,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidMessageError(
            f"payload is not JSON-serialisable: {exc}"
        ) from exc
    body = text.encode(CONTENT_ENCODING)
    _check_size(body)
    properties = pika.BasicProperties(
        content_type=CONTENT_TYPE,
        content_encoding=CONTENT_ENCODING,
        delivery_mode=pika.DeliveryMode.Persistent,
        message_id=message_id,
        type=message.type,
        # AMQP timestamps are whole seconds since the epoch.
        timestamp=int(message.timestamp.timestamp()),
        app_id=APP_ID,
    )
    return body, properties


def decode(body: bytes, properties: Properties) -> Message:
    """Parses and validates a delivery.

    Args:
        body: the raw message body.
        properties: the delivery's AMQP properties.

    Returns:
        The validated message.

    Raises:
        InvalidMessageError: if the delivery is malformed in any way. Retrying
            it cannot help, so the caller should dead-letter it.
    """
    if _media_type(properties.content_type) != CONTENT_TYPE:
        _invalid(
            f"content_type is {properties.content_type!r},"
            f" expected {CONTENT_TYPE!r}"
        )
    _check_size(body)
    try:
        envelope = json.loads(
            body.decode(CONTENT_ENCODING), parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as exc:
        # ValueError covers UnicodeDecodeError and JSONDecodeError.
        # RecursionError is what deeply nested JSON raises before 3.14.
        raise InvalidMessageError(
            f"body is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(envelope, dict):
        _invalid("body must be a JSON object")

    message_id = _canonical_id(envelope.get("message_id"))
    if (
        properties.message_id is not None
        and _canonical_id(properties.message_id) != message_id
    ):
        _invalid("message_id property does not match the body")
    type_ = _checked_type(envelope.get("type"))
    timestamp = _parse_timestamp(envelope.get("timestamp"))
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        _invalid("payload must be a JSON object")
    return Message(message_id, type_, timestamp, payload)


def _media_type(content_type: str | None) -> str | None:
    # "application/json; charset=utf-8" is still JSON: compare the media type
    # alone, case-insensitively, as RFC 9110 specifies.
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _invalid(reason: str) -> NoReturn:
    raise InvalidMessageError(reason)


def _reject_constant(name: str) -> NoReturn:
    # json accepts NaN and Infinity by default; they are not valid JSON.
    _invalid(f"{name} is not valid JSON")


def _canonical_id(value: object) -> str:
    # Deduplication compares strings, so "{ABC...}" and "abc..." must not
    # count as different messages. Normalise to the lowercase hyphenated form.
    if not isinstance(value, str):
        _invalid("message_id must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        _invalid(f"message_id {value!r} is not a UUID")


def _checked_type(value: object) -> str:
    if not isinstance(value, str) or value not in KNOWN_TYPES:
        _invalid(f"type {value!r} is not one of {sorted(KNOWN_TYPES)}")
    return value


def _check_timestamp(value: datetime.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid("timestamp must be timezone-aware")


def _parse_timestamp(value: object) -> datetime.datetime:
    if not isinstance(value, str):
        _invalid("timestamp must be an ISO 8601 string")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        _invalid(f"timestamp {value!r} is not ISO 8601")
    _check_timestamp(parsed)
    return parsed


def _check_size(body: bytes) -> None:
    if len(body) > MAX_BODY_BYTES:
        _invalid(f"body is {len(body)} bytes; limit is {MAX_BODY_BYTES}")
