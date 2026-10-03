import unittest

from pubsub.config import DEFAULT_URL, ConfigError, Settings


class FromEnvTest(unittest.TestCase):
    def test_empty_environment_gives_defaults(self) -> None:
        self.assertEqual(Settings.from_env({}), Settings())

    def test_empty_values_count_as_unset(self) -> None:
        settings = Settings.from_env(
            {"RABBITMQ_URL": "", "PUBSUB_PREFETCH": "  "}
        )
        self.assertEqual(settings.url, DEFAULT_URL)
        self.assertEqual(settings.prefetch, Settings().prefetch)

    def test_reads_every_variable(self) -> None:
        settings = Settings.from_env(
            {
                "RABBITMQ_URL": "amqps://u:p@broker.example:5671/prod",
                "PUBSUB_EXCHANGE": "ex",
                "PUBSUB_QUEUE": "q",
                "PUBSUB_ROUTING_KEY": "rk",
                "PUBSUB_DLX": "dlx",
                "PUBSUB_DLQ": "dlq",
                "PUBSUB_DELIVERY_LIMIT": "3",
                "PUBSUB_PREFETCH": "1",
                "PUBSUB_MESSAGE_COUNT": "0",
                "PUBSUB_MAX_MESSAGES": "7",
                "PUBSUB_PUBLISH_INTERVAL_MS": "25",
                "PUBSUB_WORK_MS": "100",
                "PUBSUB_HEARTBEAT_S": "10",
                "PUBSUB_RECONNECT_BASE_S": "0.1",
                "PUBSUB_RECONNECT_CAP_S": "2.5",
                "PUBSUB_LOG_LEVEL": "debug",
            }
        )
        self.assertEqual(
            settings,
            Settings(
                url="amqps://u:p@broker.example:5671/prod",
                exchange="ex",
                queue="q",
                routing_key="rk",
                dead_letter_exchange="dlx",
                dead_letter_queue="dlq",
                delivery_limit=3,
                prefetch=1,
                message_count=0,
                max_messages=7,
                publish_interval_ms=25,
                work_ms=100,
                heartbeat_s=10,
                reconnect_base_s=0.1,
                reconnect_cap_s=2.5,
                log_level="DEBUG",
            ),
        )

    def test_rejects_invalid_values(self) -> None:
        cases = {
            "not a number": ({"PUBSUB_PREFETCH": "ten"}, "PUBSUB_PREFETCH"),
            "zero prefetch": ({"PUBSUB_PREFETCH": "0"}, "PUBSUB_PREFETCH"),
            "prefetch over the AMQP limit": (
                {"PUBSUB_PREFETCH": "65536"},
                "PUBSUB_PREFETCH",
            ),
            "negative count": (
                {"PUBSUB_MESSAGE_COUNT": "-1"},
                "PUBSUB_MESSAGE_COUNT",
            ),
            "float where int expected": (
                {"PUBSUB_DELIVERY_LIMIT": "2.5"},
                "PUBSUB_DELIVERY_LIMIT",
            ),
            "nan backoff": (
                {"PUBSUB_RECONNECT_BASE_S": "nan"},
                "PUBSUB_RECONNECT_BASE_S",
            ),
            "infinite cap": (
                {"PUBSUB_RECONNECT_CAP_S": "inf"},
                "PUBSUB_RECONNECT_CAP_S",
            ),
            "cap below base": (
                {"PUBSUB_RECONNECT_BASE_S": "5", "PUBSUB_RECONNECT_CAP_S": "1"},
                "PUBSUB_RECONNECT_CAP_S",
            ),
            "http url": ({"RABBITMQ_URL": "http://localhost"}, "RABBITMQ_URL"),
            "url without host": ({"RABBITMQ_URL": "amqp://"}, "RABBITMQ_URL"),
            "url with bad port": (
                {"RABBITMQ_URL": "amqp://localhost:abc/"},
                "RABBITMQ_URL",
            ),
            "reserved exchange name": (
                {"PUBSUB_EXCHANGE": "amq.mine"},
                "PUBSUB_EXCHANGE",
            ),
            "name over 255 bytes": (
                {"PUBSUB_QUEUE": "q" * 256},
                "PUBSUB_QUEUE",
            ),
            "queue equals dlq": (
                {"PUBSUB_QUEUE": "same", "PUBSUB_DLQ": "same"},
                "PUBSUB_DLQ",
            ),
            "exchange equals dlx": (
                {"PUBSUB_EXCHANGE": "same", "PUBSUB_DLX": "same"},
                "PUBSUB_DLX",
            ),
            "unknown log level": (
                {"PUBSUB_LOG_LEVEL": "LOUD"},
                "PUBSUB_LOG_LEVEL",
            ),
            "work longer than half the heartbeat": (
                {"PUBSUB_WORK_MS": "5000", "PUBSUB_HEARTBEAT_S": "10"},
                "PUBSUB_WORK_MS",
            ),
        }
        for name, (env, variable) in cases.items():
            with self.subTest(name), self.assertRaises(ConfigError) as caught:
                Settings.from_env(env)
            self.assertIn(variable, str(caught.exception))

    def test_reports_every_problem_at_once(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            Settings.from_env(
                {
                    "PUBSUB_PREFETCH": "0",
                    "PUBSUB_MESSAGE_COUNT": "lots",
                    "RABBITMQ_URL": "nope",
                }
            )
        message = str(caught.exception)
        for variable in (
            "PUBSUB_PREFETCH",
            "PUBSUB_MESSAGE_COUNT",
            "RABBITMQ_URL",
        ):
            self.assertIn(variable, message)


class RedactedUrlTest(unittest.TestCase):
    def test_hides_the_password(self) -> None:
        settings = Settings(url="amqp://user:s3cret@host:5672/%2F")
        self.assertEqual(
            settings.redacted_url(), "amqp://user:***@host:5672/%2F"
        )

    def test_leaves_urls_without_password_alone(self) -> None:
        settings = Settings(url="amqp://host/")
        self.assertEqual(settings.redacted_url(), "amqp://host/")


if __name__ == "__main__":
    unittest.main()
