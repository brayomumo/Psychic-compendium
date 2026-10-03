"""Tests against a real RabbitMQ: ``make test-integration``.

Skipped unless PUBSUB_INTEGRATION=1 and the broker at RABBITMQ_URL answers.
Every test gets its own uniquely named exchanges and queues and deletes them
afterwards, so tests cannot see each other's messages.
"""

import io
import os
import shutil
import signal
import subprocess
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

import pika
import pika.exceptions as amqp
from procs import Proc

from pubsub import broker
from pubsub.config import DEFAULT_URL, Settings
from pubsub.consumer import Consumer
from pubsub.messages import USER_CREATED, Message, encode
from pubsub.shutdown import Shutdown

URL = os.environ.get("RABBITMQ_URL") or DEFAULT_URL
CONTAINER = os.environ.get("PUBSUB_BROKER_CONTAINER", "")
WAIT_S = 60.0
# Long simulated work needs a heartbeat timeout over twice as long, and DEBUG
# logging shows the "processing <id>" line the tests wait for.
SLOW_WORK = {"PUBSUB_HEARTBEAT_S": "60", "PUBSUB_LOG_LEVEL": "DEBUG"}


def setUpModule() -> None:
    if os.environ.get("PUBSUB_INTEGRATION") != "1":
        raise unittest.SkipTest(
            "set PUBSUB_INTEGRATION=1 (make test-integration)"
        )
    try:
        pika.BlockingConnection(pika.URLParameters(URL)).close()
    except (amqp.AMQPError, OSError) as exc:
        raise unittest.SkipTest(f"no broker at {URL}: {exc!r}") from exc


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    """Polls ``condition`` until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class BrokerTestCase(unittest.TestCase):
    """Gives each test isolated topology names and an admin channel."""

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.env = {
            "RABBITMQ_URL": URL,
            "PUBSUB_EXCHANGE": f"it.{suffix}.events",
            "PUBSUB_QUEUE": f"it.{suffix}.worker",
            "PUBSUB_DLX": f"it.{suffix}.dlx",
            "PUBSUB_DLQ": f"it.{suffix}.dlq",
            "PUBSUB_RECONNECT_BASE_S": "0.1",
            "PUBSUB_RECONNECT_CAP_S": "1",
        }
        self.settings = Settings.from_env(self.env)
        self._connection: Any = None
        self._channel: Any = None
        self._procs: list[Proc] = []
        self.addCleanup(self._clean_up)

    def spawn(self, module: str, **overrides: str) -> Proc:
        """Starts an entry point with this test's names plus overrides."""
        proc = Proc(module, {**self.env, **overrides})
        self._procs.append(proc)
        return proc

    @property
    def channel(self) -> Any:
        """An admin channel, reopened if a test or a restart closed it."""
        if self._channel is None or not self._channel.is_open:
            broker.close_quietly(self._connection)
            deadline = time.monotonic() + WAIT_S
            while True:
                try:
                    self._connection = pika.BlockingConnection(
                        pika.URLParameters(URL)
                    )
                    break
                except (amqp.AMQPError, OSError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.5)
            self._channel = self._connection.channel()
            self._channel.confirm_delivery()
        return self._channel

    def depth(self, queue: str) -> int:
        """Ready (not in-flight) messages in a queue."""
        declared = self.channel.queue_declare(queue, passive=True)
        return int(declared.method.message_count)

    def declare(self, settings: Settings | None = None) -> None:
        broker.declare_topology(self.channel, settings or self.settings)

    def publish_raw(self, body: bytes, properties: Any) -> None:
        self.channel.basic_publish(
            self.settings.exchange,
            self.settings.routing_key,
            body,
            properties,
            mandatory=True,
        )

    def finish(
        self, proc: Proc, expected: int, timeout: float = WAIT_S
    ) -> None:
        self.assertEqual(proc.finish(timeout), expected, proc.logs())

    def _clean_up(self) -> None:
        for proc in self._procs:
            proc.kill()
        channel = self.channel
        for queue in (self.settings.queue, self.settings.dead_letter_queue):
            channel.queue_delete(queue)
        for exchange in (
            self.settings.exchange,
            self.settings.dead_letter_exchange,
        ):
            channel.exchange_delete(exchange)
        broker.close_quietly(self._connection)


class DeliveryTest(BrokerTestCase):
    def test_publisher_started_first_loses_nothing(self) -> None:
        # The first version dropped everything published before a consumer
        # had bound its queue. Now the publisher declares the topology too.
        publisher = self.spawn("pubsub.publisher", PUBSUB_MESSAGE_COUNT="50")
        self.finish(publisher, 0)
        self.assertEqual(len(publisher.stdout), 50)
        self.assertEqual(self.depth(self.settings.queue), 50)

        consumer = self.spawn("pubsub.consumer", PUBSUB_MAX_MESSAGES="50")
        self.finish(consumer, 0)
        self.assertEqual(set(consumer.stdout), set(publisher.stdout))
        self.assertEqual(self.depth(self.settings.queue), 0)

    def test_consumer_started_first_receives_everything(self) -> None:
        consumer = self.spawn("pubsub.consumer", PUBSUB_MAX_MESSAGES="50")
        self.assertTrue(
            consumer.wait_for_log("consuming from"), consumer.logs()
        )
        publisher = self.spawn("pubsub.publisher", PUBSUB_MESSAGE_COUNT="50")
        self.finish(publisher, 0)
        self.finish(consumer, 0)
        self.assertEqual(sorted(consumer.stdout), sorted(publisher.stdout))

    def test_demo_passes_end_to_end(self) -> None:
        demo = self.spawn("pubsub.demo", PUBSUB_MESSAGE_COUNT="20")
        self.finish(demo, 0, timeout=120)
        output = "\n".join(demo.stdout)
        self.assertIn("checks:", output)
        self.assertNotIn("FAIL", output)


class PoisonMessageTest(BrokerTestCase):
    def test_malformed_messages_are_dead_lettered(self) -> None:
        self.declare()
        json_props = pika.BasicProperties(content_type="application/json")
        bad = [
            (b"this is not JSON", json_props),
            (b'{"message_id": "x"}', json_props),
            (b"{}", pika.BasicProperties(content_type="text/plain")),
            (
                b'{"message_id": "6f1c9a52-7f3e-4c2b-9d61-0a4c4f9e2b11",'
                b' "type": "user.deleted", "timestamp": "2026-10-02T12:00:00Z",'
                b' "payload": {}}',
                json_props,
            ),
        ]
        for body, properties in bad:
            self.publish_raw(body, properties)
        good = Message.create(USER_CREATED, {"user_id": 1})
        self.publish_raw(*encode(good))

        consumer = self.spawn("pubsub.consumer", PUBSUB_MAX_MESSAGES="1")
        self.finish(consumer, 0)

        self.assertEqual(consumer.stdout, [good.message_id])
        dlq = self.settings.dead_letter_queue
        self.assertTrue(wait_for(lambda: self.depth(dlq) == len(bad)))
        method, properties, _ = self.channel.basic_get(dlq, auto_ack=True)
        self.assertIsNotNone(method)
        death = properties.headers["x-death"][0]
        self.assertEqual(death["reason"], "rejected")
        self.assertEqual(death["queue"], self.settings.queue)

    def test_a_message_that_keeps_failing_hits_the_delivery_limit(self) -> None:
        # A handler that always raises gets the message requeued; the quorum
        # queue counts the attempts and dead-letters it at the limit.
        self.env["PUBSUB_DELIVERY_LIMIT"] = "2"
        settings = Settings.from_env(self.env)
        self.declare(settings)

        def always_fails(_message: Message) -> None:
            raise RuntimeError("downstream is broken")

        shutdown = Shutdown()
        consumer = Consumer(settings, shutdown, always_fails, out=io.StringIO())
        worker = threading.Thread(target=consumer.run)
        with self.assertLogs("pubsub.consumer", "ERROR"):
            worker.start()
            self.publish_raw(*encode(Message.create(USER_CREATED, {})))
            dlq = settings.dead_letter_queue
            reached = wait_for(lambda: self.depth(dlq) == 1)
            shutdown.request()
            worker.join(WAIT_S)
        self.assertTrue(reached)
        self.assertFalse(worker.is_alive())
        self.assertEqual(consumer.report.retried, 3, "first try + 2 retries")
        _, properties, _ = self.channel.basic_get(dlq, auto_ack=True)
        self.assertEqual(
            properties.headers["x-death"][0]["reason"], "delivery_limit"
        )


class CrashAndShutdownTest(BrokerTestCase):
    def publish(self, count: int) -> list[str]:
        publisher = self.spawn(
            "pubsub.publisher", PUBSUB_MESSAGE_COUNT=str(count)
        )
        self.finish(publisher, 0)
        return publisher.stdout

    def test_consumer_killed_mid_message_gets_it_redelivered(self) -> None:
        [message_id] = self.publish(1)
        doomed = self.spawn(
            "pubsub.consumer", PUBSUB_WORK_MS="10000", **SLOW_WORK
        )
        self.assertTrue(doomed.wait_for_log(f"processing {message_id}"))
        doomed.signal(signal.SIGKILL)
        self.finish(doomed, -signal.SIGKILL)
        self.assertEqual(doomed.stdout, [], "never acked, never reported")

        survivor = self.spawn(
            "pubsub.consumer", PUBSUB_MAX_MESSAGES="1", **SLOW_WORK
        )
        self.finish(survivor, 0)
        self.assertEqual(survivor.stdout, [message_id])
        self.assertIn("redelivered=True", survivor.logs())

    def test_sigint_finishes_the_message_in_flight_then_exits(self) -> None:
        ids = self.publish(3)
        consumer = self.spawn(
            "pubsub.consumer", PUBSUB_WORK_MS="2000", **SLOW_WORK
        )
        self.assertTrue(consumer.wait_for_log(f"processing {ids[0]}"))
        stopped_at = time.monotonic()
        consumer.signal(signal.SIGINT)
        self.finish(consumer, 130)

        self.assertLess(time.monotonic() - stopped_at, 5)
        self.assertEqual(consumer.stdout, [ids[0]], "in-flight one finished")
        # The two prefetched but unstarted messages went back to the queue.
        self.assertTrue(wait_for(lambda: self.depth(self.settings.queue) == 2))

    def test_second_sigint_forces_an_immediate_exit(self) -> None:
        self.publish(1)
        consumer = self.spawn(
            "pubsub.consumer", PUBSUB_WORK_MS="20000", **SLOW_WORK
        )
        self.assertTrue(consumer.wait_for_log("processing"))
        stopped_at = time.monotonic()
        consumer.signal(signal.SIGINT)
        # POSIX merges a signal that arrives while an identical one is still
        # pending, so wait until the first has been handled.
        self.assertTrue(consumer.wait_for_log("repeat to quit now"))
        consumer.signal(signal.SIGINT)
        self.finish(consumer, 130, timeout=5)

        self.assertLess(time.monotonic() - stopped_at, 3)
        self.assertEqual(consumer.stdout, [])
        # Unacked, so the broker puts it back when the socket closes.
        self.assertTrue(wait_for(lambda: self.depth(self.settings.queue) == 1))

    def test_publisher_sigint_stops_after_a_confirmed_message(self) -> None:
        publisher = self.spawn(
            "pubsub.publisher",
            PUBSUB_MESSAGE_COUNT="100000",
            PUBSUB_PUBLISH_INTERVAL_MS="5",
        )
        self.assertTrue(publisher.wait_for_results(20))
        publisher.signal(signal.SIGINT)
        self.finish(publisher, 130)
        # Every printed id is in the queue, and nothing beyond them.
        confirmed = len(publisher.stdout)
        self.assertEqual(self.depth(self.settings.queue), confirmed)


class MisconfigurationTest(BrokerTestCase):
    def test_wrong_password_fails_fast_instead_of_retrying(self) -> None:
        wrong = URL.replace("guest:guest@", "guest:wrong@")
        self.assertNotEqual(wrong, URL, "test assumes the guest account")
        consumer = self.spawn("pubsub.consumer", RABBITMQ_URL=wrong)
        self.finish(consumer, 1, timeout=15)
        self.assertIn("giving up", consumer.logs())
        self.assertNotIn("retry", consumer.logs())

    def test_queue_declared_with_other_arguments_is_reported(self) -> None:
        self.declare(
            Settings.from_env({**self.env, "PUBSUB_DELIVERY_LIMIT": "3"})
        )
        consumer = self.spawn("pubsub.consumer", PUBSUB_DELIVERY_LIMIT="4")
        self.finish(consumer, 1, timeout=15)
        self.assertIn("PRECONDITION_FAILED", consumer.logs())


@unittest.skipUnless(
    CONTAINER and shutil.which("docker"), "needs the docker broker container"
)
class BrokerRestartTest(BrokerTestCase):
    def test_broker_restart_mid_stream_loses_nothing_confirmed(self) -> None:
        count = 150
        consumer = self.spawn("pubsub.consumer", PUBSUB_MAX_MESSAGES=str(count))
        self.assertTrue(
            consumer.wait_for_log("consuming from"), consumer.logs()
        )
        publisher = self.spawn(
            "pubsub.publisher",
            PUBSUB_MESSAGE_COUNT=str(count),
            PUBSUB_PUBLISH_INTERVAL_MS="20",
        )
        self.assertTrue(publisher.wait_for_results(30), publisher.logs())

        subprocess.run(
            ["docker", "restart", CONTAINER],
            check=True,
            capture_output=True,
            timeout=120,
        )

        self.finish(publisher, 0, timeout=120)
        self.finish(consumer, 0, timeout=120)
        confirmed = set(publisher.stdout)
        processed = set(consumer.stdout)
        self.assertEqual(len(confirmed), count)
        self.assertEqual(confirmed - processed, set(), "confirmed but lost")
        self.assertIn("connection lost", publisher.logs() + consumer.logs())


if __name__ == "__main__":
    unittest.main()
