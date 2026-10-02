import datetime
import json
import unittest
import uuid
from typing import Any

from fakes import Props

from pubsub.messages import (
    APP_ID,
    CONTENT_TYPE,
    MAX_BODY_BYTES,
    USER_CREATED,
    InvalidMessageError,
    Message,
    decode,
    encode,
)

NOW = datetime.datetime(2026, 10, 2, 12, 30, tzinfo=datetime.UTC)


def envelope(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": "6f1c9a52-7f3e-4c2b-9d61-0a4c4f9e2b11",
        "type": USER_CREATED,
        "timestamp": NOW.isoformat(),
        "payload": {"user_id": 1},
    }
    body.update(overrides)
    return body


def raw(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode()


class EncodeTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        message = Message.create(USER_CREATED, {"user_id": 7}, now=NOW)
        body, properties = encode(message)
        self.assertEqual(decode(body, properties), message)

    def test_sets_amqp_properties_from_the_envelope(self) -> None:
        message = Message.create(USER_CREATED, {}, now=NOW)
        _, properties = encode(message)
        self.assertEqual(properties.content_type, CONTENT_TYPE)
        self.assertEqual(properties.content_encoding, "utf-8")
        self.assertEqual(properties.delivery_mode, 2, "must be persistent")
        self.assertEqual(properties.message_id, message.message_id)
        self.assertEqual(properties.type, USER_CREATED)
        self.assertEqual(properties.timestamp, int(NOW.timestamp()))
        self.assertEqual(properties.app_id, APP_ID)

    def test_create_uses_fresh_uuids(self) -> None:
        ids = {Message.create(USER_CREATED, {}).message_id for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_refuses_what_decode_would_reject(self) -> None:
        bad = {
            "unknown type": Message("x", "user.deleted", NOW, {}),
            "naive timestamp": Message(
                str(uuid.uuid4()), USER_CREATED, NOW.replace(tzinfo=None), {}
            ),
            "non-uuid id": Message("abc", USER_CREATED, NOW, {}),
            "unserialisable payload": Message(
                str(uuid.uuid4()), USER_CREATED, NOW, {"x": object()}
            ),
            "nan in payload": Message(
                str(uuid.uuid4()), USER_CREATED, NOW, {"x": float("nan")}
            ),
            "oversized": Message(
                str(uuid.uuid4()),
                USER_CREATED,
                NOW,
                {"x": "a" * MAX_BODY_BYTES},
            ),
        }
        for name, message in bad.items():
            with self.subTest(name), self.assertRaises(InvalidMessageError):
                encode(message)

    def test_keeps_non_ascii_text(self) -> None:
        message = Message.create(USER_CREATED, {"name": "Mũmo"}, now=NOW)
        body, properties = encode(message)
        self.assertIn("Mũmo".encode(), body)
        self.assertEqual(decode(body, properties).payload["name"], "Mũmo")


class DecodeTest(unittest.TestCase):
    def test_accepts_a_valid_delivery(self) -> None:
        message = decode(raw(envelope()), Props())
        self.assertEqual(message.type, USER_CREATED)
        self.assertEqual(message.timestamp, NOW)
        self.assertEqual(message.payload, {"user_id": 1})

    def test_normalises_the_id_so_dedupe_sees_one_message(self) -> None:
        canonical = "6f1c9a52-7f3e-4c2b-9d61-0a4c4f9e2b11"
        for spelling in (
            canonical.upper(),
            "{" + canonical + "}",
            canonical.replace("-", ""),
        ):
            with self.subTest(spelling):
                message = decode(raw(envelope(message_id=spelling)), Props())
                self.assertEqual(message.message_id, canonical)

    def test_accepts_content_type_parameters(self) -> None:
        for content_type in (
            "application/json; charset=utf-8",
            "Application/JSON",
        ):
            with self.subTest(content_type):
                decode(raw(envelope()), Props(content_type))

    def test_ignores_unknown_fields(self) -> None:
        # Tolerant reader: a newer producer may add fields.
        decode(raw(envelope(trace_id="abc")), Props())

    def test_rejects_malformed_deliveries(self) -> None:
        cases: dict[str, tuple[bytes, Props]] = {
            "wrong content type": (raw(envelope()), Props("text/plain")),
            "json lookalike": (raw(envelope()), Props("application/jsonp")),
            "missing content type": (raw(envelope()), Props(None)),
            "not json": (b"this is not JSON", Props()),
            "not utf-8": (b"\xff\xfe", Props()),
            "json array": (b"[1, 2]", Props()),
            "NaN literal": (
                raw(envelope()).replace(b'"user_id": 1', b'"user_id": NaN'),
                Props(),
            ),
            # Under the size cap. Python 3.11-3.13 raise RecursionError;
            # 3.14's parser copes, and the result is not an object. Either
            # way it is rejected, never a crash.
            "deeply nested": (b"[" * 20_000 + b"]" * 20_000, Props()),
            "oversized": (b" " * (MAX_BODY_BYTES + 1), Props()),
            "missing id": (raw(envelope(message_id=None)), Props()),
            "numeric id": (raw(envelope(message_id=42)), Props()),
            "non-uuid id": (raw(envelope(message_id="abc")), Props()),
            "id mismatch with property": (
                raw(envelope()),
                Props(message_id=str(uuid.uuid4())),
            ),
            "unknown type": (raw(envelope(type="user.deleted")), Props()),
            "missing type": (raw(envelope(type=None)), Props()),
            "naive timestamp": (
                raw(envelope(timestamp="2026-10-02T12:30:00")),
                Props(),
            ),
            "garbage timestamp": (raw(envelope(timestamp="noon")), Props()),
            "numeric timestamp": (raw(envelope(timestamp=1)), Props()),
            "payload not an object": (raw(envelope(payload=[1])), Props()),
            "missing payload": (raw(envelope(payload=None)), Props()),
        }
        for name, (body, properties) in cases.items():
            with self.subTest(name), self.assertRaises(InvalidMessageError):
                decode(body, properties)


if __name__ == "__main__":
    unittest.main()
