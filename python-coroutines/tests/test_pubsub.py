"""Tests for the coroutine pub/sub broker."""

import contextlib
import inspect
import io
import unittest
from collections.abc import Callable, Generator
from typing import Any

import pubsub
from prime import Sink, coroutine
from pubsub import Broker, BrokerClosedError, Message


@coroutine
def recorder(into: list[Message]) -> Generator[None, Message, None]:
    """Append every message received to ``into``."""
    while True:
        into.append((yield))


@coroutine
def on_message(
    action: Callable[[Message], object],
) -> Generator[None, Message, None]:
    """Call ``action`` with every message received."""
    while True:
        action((yield))


def tracked(closes: list[str], name: str) -> Sink[Message]:
    """A subscriber that records its name in ``closes`` when it shuts down."""

    @coroutine
    def subscriber() -> Generator[None, Message, None]:
        try:
            while True:
                yield
        finally:
            closes.append(name)

    return subscriber()


def is_closed(gen: Generator[Any, Any, Any]) -> bool:
    return inspect.getgeneratorstate(gen) == inspect.GEN_CLOSED


class DeliveryTest(unittest.TestCase):
    def test_delivers_to_topic_subscribers_in_subscription_order(self) -> None:
        log: list[str] = []

        def logs_as(name: str) -> Callable[[Message], None]:
            return lambda _: log.append(name)

        with Broker() as broker:
            for name in ("a", "b", "c"):
                broker.subscribe("t", on_message(logs_as(name)))
            broker.publish("t", 1)
        self.assertEqual(log, ["a", "b", "c"])

    def test_subscribers_only_receive_their_topics(self) -> None:
        temps: list[Message] = []
        both: list[Message] = []
        with Broker() as broker:
            broker.subscribe("temp", recorder(temps))
            everything = recorder(both)
            broker.subscribe("temp", everything)
            broker.subscribe("hum", everything)
            broker.publish("temp", 20)
            broker.publish("hum", 50)
            broker.publish("other", 0)
        self.assertEqual(temps, [Message("temp", 20)])
        self.assertEqual(both, [Message("temp", 20), Message("hum", 50)])

    def test_publish_without_subscribers_is_dropped(self) -> None:
        with Broker() as broker:
            broker.publish("nobody", 1)


class IsolationTest(unittest.TestCase):
    def test_raising_subscriber_is_removed_and_others_still_receive(
        self,
    ) -> None:
        calls = 0

        def explode(_: Message) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("subscriber bug")

        before: list[Message] = []
        after: list[Message] = []
        with Broker() as broker:
            broker.subscribe("t", recorder(before))
            broker.subscribe("t", on_message(explode))
            broker.subscribe("t", recorder(after))
            with self.assertLogs("pubsub", "ERROR") as logs:
                broker.publish("t", 1)
            broker.publish("t", 2)
        self.assertEqual(calls, 1, "a dead subscriber must not be retried")
        self.assertEqual([m.payload for m in before], [1, 2])
        self.assertEqual([m.payload for m in after], [1, 2])
        self.assertIn("subscriber bug", "\n".join(logs.output))

    def test_finished_subscriber_is_removed_quietly(self) -> None:
        @coroutine
        def first_only(into: list[object]) -> Generator[None, Message, None]:
            message = yield
            into.append(message.payload)

        got: list[object] = []
        with Broker() as broker:
            broker.subscribe("t", first_only(got))
            with self.assertLogs("pubsub", "INFO") as logs:
                broker.publish("t", 1)
            broker.publish("t", 2)
        self.assertEqual(got, [1])
        self.assertIn("finished", logs.output[0])

    def test_non_numeric_payload_kills_only_the_mean_tracker(self) -> None:
        got: list[Message] = []
        with (
            contextlib.redirect_stdout(io.StringIO()),
            Broker() as broker,
        ):
            broker.subscribe("t", pubsub.mean_tracker("mean"))
            broker.subscribe("t", recorder(got))
            with self.assertLogs("pubsub", "ERROR"):
                broker.publish("t", "not a number")
            broker.publish("t", 3)
        self.assertEqual([m.payload for m in got], ["not a number", 3])


class ShutdownTest(unittest.TestCase):
    def test_walrus_never_stops_bug_close_stops_subscribers(self) -> None:
        # The first version looped on `while running := True`, which reset
        # the flag every pass, so its consumer could never stop. Shutdown is
        # now close(): GeneratorExit at the yield, cleanup in finally.
        closes: list[str] = []
        subscriber = tracked(closes, "s")
        broker = Broker()
        broker.subscribe("t", subscriber)
        broker.close()
        self.assertTrue(is_closed(subscriber))
        self.assertEqual(closes, ["s"])

    def test_close_closes_multi_topic_subscriber_once(self) -> None:
        closes: list[str] = []
        shared = tracked(closes, "shared")
        broker = Broker()
        broker.subscribe("a", shared)
        broker.subscribe("b", shared)
        broker.subscribe("a", tracked(closes, "solo"))
        broker.close()
        self.assertEqual(sorted(closes), ["shared", "solo"])

    def test_close_is_idempotent(self) -> None:
        closes: list[str] = []
        broker = Broker()
        broker.subscribe("t", tracked(closes, "s"))
        broker.close()
        broker.close()
        self.assertTrue(broker.closed)
        self.assertEqual(closes, ["s"])

    def test_use_after_close_raises(self) -> None:
        broker = Broker()
        broker.close()
        with self.assertRaises(BrokerClosedError):
            broker.publish("t", 1)
        with self.assertRaises(BrokerClosedError):
            broker.subscribe("t", recorder([]))

    def test_context_manager_closes_when_block_raises(self) -> None:
        closes: list[str] = []
        with self.assertRaises(KeyError), Broker() as broker:
            broker.subscribe("t", tracked(closes, "s"))
            raise KeyError("boom")
        self.assertEqual(closes, ["s"])

    def test_failing_cleanup_does_not_stop_other_closes(self) -> None:
        @coroutine
        def bad_cleanup() -> Generator[None, Message, None]:
            try:
                while True:
                    yield
            finally:
                raise OSError("cleanup failed")

        closes: list[str] = []
        broker = Broker()
        broker.subscribe("t", bad_cleanup())
        broker.subscribe("t", tracked(closes, "after"))
        with self.assertLogs("pubsub", "ERROR") as logs:
            broker.close()
        self.assertEqual(closes, ["after"])
        self.assertIn("cleanup failed", "\n".join(logs.output))

    def test_closing_broker_from_inside_subscriber_is_refused(self) -> None:
        broker = Broker()
        broker.subscribe("t", on_message(lambda _: broker.close()))
        with self.assertLogs("pubsub", "ERROR") as logs:
            broker.publish("t", 1)
        self.assertFalse(broker.closed)
        self.assertIn("from inside a subscriber", "\n".join(logs.output))
        broker.close()


class SubscriptionTest(unittest.TestCase):
    def test_rejects_unprimed_finished_and_duplicate_subscribers(self) -> None:
        def unprimed() -> Generator[None, Message, None]:
            while True:
                yield

        finished = recorder([])
        finished.close()
        subscriber = recorder([])
        with Broker() as broker:
            broker.subscribe("t", subscriber)
            cases: dict[str, tuple[str, Sink[Message]]] = {
                "unprimed": ("t", unprimed()),
                "finished": ("t", finished),
                "duplicate": ("t", subscriber),
                "empty topic": ("", recorder([])),
            }
            for name, (topic, sub) in cases.items():
                with self.subTest(name), self.assertRaises(ValueError):
                    broker.subscribe(topic, sub)

    def test_unsubscribe_closes_only_after_last_topic(self) -> None:
        closes: list[str] = []
        subscriber = tracked(closes, "s")
        with Broker() as broker:
            broker.subscribe("a", subscriber)
            broker.subscribe("b", subscriber)
            broker.unsubscribe("a", subscriber)
            self.assertEqual(closes, [])
            broker.unsubscribe("b", subscriber)
            self.assertEqual(closes, ["s"])
            with self.assertRaises(ValueError):
                broker.unsubscribe("b", subscriber)

    def test_unsubscribed_topic_stops_delivery(self) -> None:
        got: list[Message] = []
        subscriber = recorder(got)
        with Broker() as broker:
            broker.subscribe("a", subscriber)
            broker.subscribe("b", subscriber)
            broker.unsubscribe("a", subscriber)
            broker.publish("a", 1)
            broker.publish("b", 2)
        self.assertEqual(got, [Message("b", 2)])

    def test_subscriber_unsubscribing_itself_is_refused(self) -> None:
        broker = Broker()
        holder: list[Sink[Message]] = []
        holder.append(on_message(lambda _: broker.unsubscribe("t", holder[0])))
        broker.subscribe("t", holder[0])
        with self.assertLogs("pubsub", "ERROR") as logs:
            broker.publish("t", 1)
        self.assertIn("cannot unsubscribe itself", "\n".join(logs.output))
        broker.close()

    def test_subscriber_can_unsubscribe_a_later_one_mid_delivery(
        self,
    ) -> None:
        later: list[Message] = []
        victim = recorder(later)
        with Broker() as broker:
            broker.subscribe(
                "t", on_message(lambda _: broker.unsubscribe("t", victim))
            )
            broker.subscribe("t", victim)
            broker.publish("t", 1)
        self.assertEqual(later, [])
        self.assertTrue(is_closed(victim))


class ReentrancyTest(unittest.TestCase):
    def test_publish_from_subscriber_is_queued_in_order(self) -> None:
        log: list[str] = []

        def relay(message: Message) -> None:
            log.append(f"relay got {message.payload}")
            if message.payload == 1:
                broker.publish("t", 2)
                log.append("relay published 2")

        with Broker() as broker:
            broker.subscribe("t", on_message(relay))
            broker.subscribe(
                "t", on_message(lambda m: log.append(f"tail got {m.payload}"))
            )
            broker.publish("t", 1)
        self.assertEqual(
            log,
            [
                "relay got 1",
                "relay published 2",
                "tail got 1",
                "relay got 2",
                "tail got 2",
            ],
        )

    def test_runaway_republish_loop_is_bounded_and_removed(self) -> None:
        received: list[object] = []

        def republish(message: Message) -> None:
            received.append(message.payload)
            assert isinstance(message.payload, int)
            broker.publish("t", message.payload + 1)

        broker = Broker(max_cascade=10)
        broker.subscribe("t", on_message(republish))
        with self.assertLogs("pubsub", "ERROR") as logs:
            broker.publish("t", 0)
        self.assertEqual(received, list(range(11)))
        self.assertIn("BrokerOverflowError", "\n".join(logs.output))
        broker.publish("t", 100)  # cascade budget resets per publish
        self.assertEqual(received, list(range(11)))
        broker.close()

    def test_invalid_max_cascade_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Broker(max_cascade=0)


class DemoTest(unittest.TestCase):
    def test_sensor_readings_are_bounded_and_reproducible(self) -> None:
        first = list(pubsub.sensor_readings(5, seed=1))
        self.assertEqual(first, list(pubsub.sensor_readings(5, seed=1)))
        self.assertEqual(len(first), 5)
        self.assertEqual(list(pubsub.sensor_readings(0, seed=1)), [])
        with self.assertRaises(ValueError):
            list(pubsub.sensor_readings(-1, seed=1))

    def test_demo_runs_and_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertLogs(level="INFO"):
            self.assertEqual(pubsub.main([]), 0)
        self.assertIn("temp-mean closed: 3 readings", out.getvalue())


if __name__ == "__main__":
    unittest.main()
