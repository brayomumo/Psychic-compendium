import dataclasses
import io
import json
import unittest
from collections.abc import Iterator

import pika.exceptions as amqp
from fakes import FakeChannel, FakeConnection, SessionScript, session

from pubsub.config import Settings
from pubsub.messages import USER_CREATED, Message
from pubsub.publisher import (
    MAX_REFUSALS,
    Publisher,
    PublishError,
    demo_messages,
)
from pubsub.shutdown import Shutdown

FAST = Settings(reconnect_base_s=0.001, reconnect_cap_s=0.001)


def messages(count: int) -> list[Message]:
    return list(demo_messages(count))


class Harness:
    def __init__(
        self, sessions: SessionScript, settings: Settings = FAST
    ) -> None:
        self.shutdown = Shutdown()
        self.out = io.StringIO()
        self.publisher = Publisher(
            settings, self.shutdown, open_session=sessions, out=self.out
        )

    @property
    def printed(self) -> list[str]:
        return self.out.getvalue().split()


def published_ids(channel: FakeChannel) -> list[str]:
    return [
        json.loads(body)["message_id"] for _, _, body, _, _ in channel.published
    ]


class PublishAllTest(unittest.TestCase):
    def test_publishes_persistent_mandatory_messages_in_order(self) -> None:
        channel = FakeChannel()
        harness = Harness(SessionScript(session(channel)))
        batch = messages(3)

        report = harness.publisher.publish_all(batch)

        expected = [m.message_id for m in batch]
        self.assertEqual(published_ids(channel), expected)
        self.assertEqual(harness.printed, expected)
        self.assertEqual(report.confirmed, 3)
        self.assertFalse(report.interrupted)
        for exchange, key, _, properties, mandatory in channel.published:
            self.assertEqual((exchange, key), (FAST.exchange, FAST.routing_key))
            self.assertTrue(mandatory, "unroutable messages must come back")
            self.assertEqual(properties.delivery_mode, 2)

    def test_zero_messages_never_connects(self) -> None:
        sessions = SessionScript(session())
        report = Harness(sessions).publisher.publish_all([])
        self.assertEqual(sessions.opened, 0)
        self.assertEqual(report.confirmed, 0)

    def test_closes_its_connection_when_done(self) -> None:
        connection = FakeConnection()
        Harness(
            SessionScript(session(connection=connection))
        ).publisher.publish_all(messages(1))
        self.assertTrue(connection.closed)

    def test_republishes_the_same_message_after_a_lost_confirm(self) -> None:
        # The confirm never arrived, so the outcome is unknown: publish the
        # same message, with the same id, on a new connection.
        first = FakeChannel(iter([amqp.StreamLostError("reset")]))
        second = FakeChannel()
        sessions = SessionScript(session(first), session(second))
        harness = Harness(sessions)
        batch = messages(2)

        with self.assertLogs("pubsub.publisher", "WARNING"):
            report = harness.publisher.publish_all(batch)

        self.assertEqual(sessions.opened, 2)
        self.assertEqual(published_ids(second), [m.message_id for m in batch])
        self.assertEqual(harness.printed, [m.message_id for m in batch])
        self.assertEqual(report.republished, 1)
        self.assertEqual(report.confirmed, 2)

    def test_unroutable_message_triggers_a_topology_redeclare(self) -> None:
        first = FakeChannel(iter([amqp.UnroutableError([])]))
        second = FakeChannel()
        sessions = SessionScript(session(first), session(second))

        with self.assertLogs("pubsub.publisher", "WARNING"):
            report = Harness(sessions).publisher.publish_all(messages(1))

        self.assertEqual(sessions.opened, 2, "reconnecting re-declares")
        self.assertEqual(report.confirmed, 1)

    def test_nack_is_retried_on_the_same_connection(self) -> None:
        channel = FakeChannel(iter([amqp.NackError([])]))
        sessions = SessionScript(session(channel))

        with self.assertLogs("pubsub.publisher", "WARNING"):
            report = Harness(sessions).publisher.publish_all(messages(1))

        self.assertEqual(sessions.opened, 1)
        self.assertEqual(report.confirmed, 1)

    def test_gives_up_when_the_broker_keeps_refusing(self) -> None:
        channel = FakeChannel(iter([amqp.NackError([])] * MAX_REFUSALS))
        harness = Harness(SessionScript(session(channel)))
        with (
            self.assertLogs("pubsub.publisher", "WARNING"),
            self.assertRaises(PublishError),
        ):
            harness.publisher.publish_all(messages(1))
        self.assertEqual(harness.printed, [])

    def test_fatal_errors_propagate(self) -> None:
        channel = FakeChannel(
            iter([amqp.ChannelClosedByBroker(403, "ACCESS_REFUSED")])
        )
        harness = Harness(SessionScript(session(channel)))
        with self.assertRaises(amqp.ChannelClosedByBroker):
            harness.publisher.publish_all(messages(1))

    def test_stops_between_messages_when_asked(self) -> None:
        harness = Harness(SessionScript(session()))

        def stop_after_two() -> Iterator[Message]:
            for index, message in enumerate(messages(5)):
                if index == 2:
                    harness.shutdown.request()
                yield message

        report = harness.publisher.publish_all(stop_after_two())
        self.assertEqual(report.confirmed, 2)
        self.assertTrue(report.interrupted)

    def test_stop_while_reconnecting_is_an_interruption(self) -> None:
        first = FakeChannel(iter([amqp.StreamLostError("gone")]))
        harness = Harness(SessionScript(session(first)))  # No second session.

        with self.assertLogs("pubsub.publisher", "WARNING"):
            report = harness.publisher.publish_all(messages(1))

        self.assertTrue(report.interrupted)
        self.assertEqual(report.confirmed, 0)
        self.assertEqual(harness.printed, [])

    def test_interval_keeps_servicing_the_connection(self) -> None:
        connection = FakeConnection()
        settings = dataclasses.replace(FAST, publish_interval_ms=30)
        harness = Harness(
            SessionScript(session(connection=connection)), settings
        )

        report = harness.publisher.publish_all(messages(3))

        self.assertEqual(report.confirmed, 3)
        self.assertGreater(
            connection.event_calls, 0, "heartbeats must flow while idle"
        )

    def test_prints_the_canonical_id_that_went_on_the_wire(self) -> None:
        canonical = "6f1c9a52-7f3e-4c2b-9d61-0a4c4f9e2b11"
        message = dataclasses.replace(
            messages(1)[0], message_id=canonical.upper()
        )
        channel = FakeChannel()
        harness = Harness(SessionScript(session(channel)))
        harness.publisher.publish_all([message])
        self.assertEqual(harness.printed, [canonical])
        self.assertEqual(published_ids(channel), [canonical])

    def test_rejects_messages_the_consumer_could_not_decode(self) -> None:
        bad = Message("not-a-uuid", USER_CREATED, messages(1)[0].timestamp, {})
        harness = Harness(SessionScript(session()))
        with self.assertRaises(ValueError):
            harness.publisher.publish_all([bad])


if __name__ == "__main__":
    unittest.main()
