import unittest

import pika.exceptions as amqp

from pubsub import broker
from pubsub.config import Settings


class IsTransientTest(unittest.TestCase):
    def test_connection_problems_are_retried(self) -> None:
        for exc in (
            amqp.AMQPConnectionError("refused"),
            amqp.StreamLostError("eof"),
            amqp.ConnectionClosedByBroker(320, "CONNECTION_FORCED"),
            amqp.AMQPHeartbeatTimeout("missed"),
            amqp.ConnectionBlockedTimeout("alarm"),
            amqp.ConnectionWrongStateError("closed"),
            amqp.ChannelWrongStateError("closed"),
            amqp.ChannelClosedByBroker(541, "INTERNAL_ERROR"),
            ConnectionResetError(),
            # What pika raises when a restarting broker drops the socket
            # mid-handshake: named after the stage, not the cause.
            amqp.IncompatibleProtocolError(
                "StreamLostError: ('Transport indicated EOF',)"
            ),
            amqp.ProbableAuthenticationError(
                "StreamLostError: ('Transport indicated EOF',)"
            ),
            amqp.ProbableAccessDeniedError(
                "StreamLostError: ('Transport indicated EOF',)"
            ),
        ):
            with self.subTest(type(exc).__name__):
                self.assertTrue(broker.is_transient(exc))

    def test_misconfiguration_is_not_retried(self) -> None:
        for exc in (
            amqp.ProbableAuthenticationError(
                "ConnectionClosedByBroker: (403) 'ACCESS_REFUSED - Login was"
                " refused using authentication mechanism PLAIN'"
            ),
            amqp.ProbableAccessDeniedError(
                "ConnectionClosedByBroker: (530) 'NOT_ALLOWED - vhost x not"
                " found'"
            ),
            amqp.AuthenticationError("PLAIN"),
            amqp.ChannelClosedByBroker(406, "PRECONDITION_FAILED"),
            amqp.ChannelClosedByBroker(404, "NOT_FOUND"),
            amqp.ChannelClosedByBroker(403, "ACCESS_REFUSED"),
            amqp.UnroutableError([]),
            ValueError("bug"),
        ):
            with self.subTest(type(exc).__name__):
                self.assertFalse(broker.is_transient(exc))


class TopologyTest(unittest.TestCase):
    def test_main_queue_is_a_quorum_queue_with_dead_lettering(self) -> None:
        arguments = broker.queue_arguments(
            Settings(delivery_limit=7, dead_letter_exchange="dlx")
        )
        self.assertEqual(
            arguments,
            {
                "x-queue-type": "quorum",
                "x-delivery-limit": 7,
                "x-dead-letter-exchange": "dlx",
                "x-dead-letter-strategy": "at-least-once",
                "x-overflow": "reject-publish",
            },
        )


class ConnectionParametersTest(unittest.TestCase):
    def test_retries_are_left_to_us_and_heartbeats_are_on(self) -> None:
        params = broker.connection_parameters(
            Settings(url="amqp://u:p@example.com:5679/%2F", heartbeat_s=12),
            "consumer",
        )
        self.assertEqual(params.host, "example.com")
        self.assertEqual(params.port, 5679)
        self.assertEqual(params.heartbeat, 12)
        self.assertEqual(params.connection_attempts, 1)
        self.assertEqual(
            params.blocked_connection_timeout,
            broker.BLOCKED_CONNECTION_TIMEOUT_S,
        )
        self.assertTrue(
            params.client_properties["connection_name"].startswith(
                "pubsub-consumer-"
            )
        )


if __name__ == "__main__":
    unittest.main()
