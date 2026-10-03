import dataclasses
import io
import unittest
from typing import Any

import pika.exceptions as amqp
from fakes import FakeChannel, FakeConnection, Method, SessionScript, session

from pubsub.config import Settings
from pubsub.consumer import Consumer, Handler
from pubsub.messages import USER_CREATED, Message, encode
from pubsub.shutdown import Shutdown

FAST = Settings(reconnect_base_s=0.001, reconnect_cap_s=0.001)


def delivery() -> tuple[Message, bytes, Any]:
    message = Message.create(USER_CREATED, {"user_id": 1})
    body, properties = encode(message)
    return message, body, properties


def ignore(_message: Message) -> None:
    pass


class Harness:
    """A consumer wired to fakes, with its stdout captured."""

    def __init__(
        self,
        handler: Handler = ignore,
        settings: Settings = FAST,
        sessions: SessionScript | None = None,
    ) -> None:
        self.shutdown = Shutdown()
        self.out = io.StringIO()
        self.consumer = Consumer(
            settings,
            self.shutdown,
            handler,
            open_session=sessions or SessionScript(),
            out=self.out,
        )

    def deliver(
        self, channel: FakeChannel, body: bytes, properties: Any, **method: Any
    ) -> None:
        # Called the way pika calls the consumer's callback.
        self.consumer._on_message(channel, Method(**method), properties, body)

    @property
    def printed(self) -> list[str]:
        return self.out.getvalue().split()


class OnMessageTest(unittest.TestCase):
    def test_processed_message_is_acked_and_reported(self) -> None:
        handled: list[Message] = []
        harness = Harness(handled.append)
        channel = FakeChannel()
        message, body, properties = delivery()

        harness.deliver(channel, body, properties, delivery_tag=5)

        self.assertEqual(handled, [message])
        self.assertEqual(channel.acks, [5])
        self.assertEqual(channel.nacks, [])
        self.assertEqual(harness.printed, [message.message_id])
        self.assertEqual(harness.consumer.report.processed, 1)

    def test_malformed_message_is_dead_lettered_not_requeued(self) -> None:
        handled: list[Message] = []
        harness = Harness(handled.append)
        channel = FakeChannel()
        _, _, properties = delivery()

        with self.assertLogs("pubsub.consumer", "WARNING"):
            harness.deliver(channel, b"not json", properties, delivery_tag=9)

        self.assertEqual(channel.nacks, [(9, False)])
        self.assertEqual(channel.acks, [])
        self.assertEqual(handled, [])
        self.assertEqual(harness.consumer.report.dead_lettered, 1)

    def test_handler_failure_is_requeued_for_retry(self) -> None:
        def explode(_message: Message) -> None:
            raise RuntimeError("database is down")

        harness = Harness(explode)
        channel = FakeChannel()
        _, body, properties = delivery()

        with self.assertLogs("pubsub.consumer", "ERROR"):
            harness.deliver(channel, body, properties, delivery_tag=3)

        self.assertEqual(channel.nacks, [(3, True)])
        self.assertEqual(channel.acks, [])
        self.assertEqual(harness.printed, [])
        self.assertEqual(harness.consumer.report.retried, 1)

    def test_duplicate_is_acked_without_reprocessing(self) -> None:
        handled: list[Message] = []
        harness = Harness(handled.append)
        channel = FakeChannel()
        message, body, properties = delivery()

        harness.deliver(channel, body, properties, delivery_tag=1)
        harness.deliver(channel, body, properties, delivery_tag=2)

        self.assertEqual(handled, [message])
        self.assertEqual(channel.acks, [1, 2])
        self.assertEqual(harness.printed, [message.message_id])
        self.assertEqual(harness.consumer.report.duplicates, 1)

    def test_lost_ack_does_not_double_count_the_redelivery(self) -> None:
        # The ack dies with the connection, so the broker redelivers. The
        # id must still be reported exactly once.
        handled: list[Message] = []
        harness = Harness(handled.append)
        message, body, properties = delivery()

        class AckLost(FakeChannel):
            def basic_ack(
                self, delivery_tag: int = 0, multiple: bool = False
            ) -> None:
                raise amqp.StreamLostError("connection reset")

        with self.assertRaises(amqp.StreamLostError):
            harness.deliver(AckLost(), body, properties)
        retry_channel = FakeChannel()
        harness.deliver(retry_channel, body, properties, redelivered=True)

        self.assertEqual(handled, [message])
        self.assertEqual(retry_channel.acks, [1])
        self.assertEqual(harness.printed, [message.message_id])

    def test_delivery_after_shutdown_is_requeued_untouched(self) -> None:
        handled: list[Message] = []
        harness = Harness(handled.append)
        harness.shutdown.request()
        channel = FakeChannel()
        _, body, properties = delivery()

        harness.deliver(channel, body, properties, delivery_tag=4)

        self.assertEqual(channel.nacks, [(4, True)])
        self.assertEqual(handled, [])

    def test_reaching_max_messages_requests_shutdown(self) -> None:
        harness = Harness(settings=dataclasses.replace(FAST, max_messages=2))
        channel = FakeChannel()
        for tag in (1, 2):
            self.assertFalse(harness.shutdown.requested)
            _, body, properties = delivery()
            harness.deliver(channel, body, properties, delivery_tag=tag)
        self.assertTrue(harness.shutdown.requested)
        self.assertIsNone(harness.shutdown.signum)

    def test_redeliveries_are_counted(self) -> None:
        harness = Harness()
        _, body, properties = delivery()
        harness.deliver(FakeChannel(), body, properties, redelivered=True)
        self.assertEqual(harness.consumer.report.redelivered, 1)


class RunTest(unittest.TestCase):
    def test_consumes_with_manual_acks_then_cancels_and_closes(self) -> None:
        channel = FakeChannel()
        message, body, properties = delivery()
        harness: Harness

        def broker_delivers_once() -> None:
            assert channel.on_message is not None
            channel.on_message(channel, Method(), properties, body)
            harness.shutdown.request()

        connection = FakeConnection(broker_delivers_once)
        harness = Harness(sessions=SessionScript(session(channel, connection)))

        report = harness.consumer.run()

        self.assertEqual(channel.prefetch, FAST.prefetch)
        self.assertEqual(channel.acks, [1])
        self.assertEqual(channel.cancelled, ["ctag-1"])
        self.assertTrue(connection.closed)
        self.assertEqual(report.processed, 1)
        self.assertEqual(harness.printed, [message.message_id])

    def test_reconnects_after_the_connection_drops(self) -> None:
        def drop() -> None:
            raise amqp.StreamLostError("broker restarted")

        first = FakeConnection(drop)
        second_channel = FakeChannel()
        _, body, properties = delivery()
        harness: Harness

        def deliver_then_stop() -> None:
            assert second_channel.on_message is not None
            second_channel.on_message(
                second_channel, Method(), properties, body
            )
            harness.shutdown.request()

        second = FakeConnection(deliver_then_stop)
        sessions = SessionScript(
            session(connection=first), session(second_channel, second)
        )
        harness = Harness(sessions=sessions)

        with self.assertLogs("pubsub.consumer", "WARNING") as logs:
            report = harness.consumer.run()

        self.assertEqual(sessions.opened, 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(report.processed, 1)
        self.assertIn("connection lost", "\n".join(logs.output))

    def test_resubscribes_when_the_broker_cancels_the_consumer(self) -> None:
        first_channel = FakeChannel()

        def broker_cancels() -> None:
            assert first_channel.on_cancel is not None
            first_channel.on_cancel(object())

        harness: Harness

        def stop() -> None:
            harness.shutdown.request()

        sessions = SessionScript(
            session(first_channel, FakeConnection(broker_cancels)),
            session(connection=FakeConnection(stop)),
        )
        harness = Harness(sessions=sessions)

        with self.assertLogs("pubsub.consumer", "WARNING") as logs:
            harness.consumer.run()

        self.assertEqual(sessions.opened, 2)
        self.assertIn("cancelled", "\n".join(logs.output))

    def test_fatal_errors_propagate(self) -> None:
        def mismatch() -> None:
            raise amqp.ChannelClosedByBroker(406, "PRECONDITION_FAILED")

        harness = Harness(
            sessions=SessionScript(session(connection=FakeConnection(mismatch)))
        )
        with self.assertRaises(amqp.ChannelClosedByBroker):
            harness.consumer.run()

    def test_closed_stdout_is_not_mistaken_for_a_broker_outage(self) -> None:
        # `make consume | head -1`: once head exits, writes fail. That must
        # end the run, not start an endless reconnect loop.
        channel = FakeChannel()
        _, body, properties = delivery()

        class ClosedPipe(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        def deliver() -> None:
            assert channel.on_message is not None
            channel.on_message(channel, Method(), properties, body)

        sessions = SessionScript(
            session(channel, FakeConnection(deliver)), session(), session()
        )
        consumer = Consumer(
            FAST, Shutdown(), ignore, open_session=sessions, out=ClosedPipe()
        )
        with self.assertRaises(BrokenPipeError):
            consumer.run()
        self.assertEqual(sessions.opened, 1)
        self.assertEqual(channel.acks, [], "unacked, so it is redelivered")

    def test_returns_when_stopped_while_connecting(self) -> None:
        report = Harness(sessions=SessionScript()).consumer.run()
        self.assertEqual(report.processed, 0)


if __name__ == "__main__":
    unittest.main()
