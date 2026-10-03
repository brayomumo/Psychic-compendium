"""The end-to-end run behind ``make run``.

1. Delete this demo's exchanges and queues, so the run starts from nothing.
2. Run the publisher with no consumer and no queue. It declares the topology
   itself, so nothing it publishes is dropped.
3. Publish one malformed message.
4. Start the consumer, wait until it has processed every confirmed message and
   dead-lettered the malformed one, then stop it with SIGTERM.
5. Check the results, and exit 0 only if every check passed.

Results go to stdout. The publisher's and consumer's logs pass through to
stderr.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

import pika
import pika.exceptions as amqp

from pubsub import broker, cli
from pubsub.config import Settings

STEP_TIMEOUT_S = 60.0
_POLL_S = 0.1


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the demo.

    Returns:
        0 if every check passed, 1 if any failed or a step broke, 2 on bad
        configuration, 130 if interrupted with Ctrl+C.
    """
    cli.parse_args("pubsub.demo", "End-to-end RabbitMQ demo.", argv)
    settings = cli.load_settings("pubsub.demo")
    if settings is None:
        return cli.EXIT_USAGE
    cli.configure_logging(settings.log_level)
    cli.line_buffered_stdout()
    try:
        return _run(settings)
    except (amqp.AMQPError, OSError) as exc:
        _fail(
            f"broker unavailable ({broker.describe(exc)}); try `make broker-up`"
        )
    except subprocess.CalledProcessError as exc:
        _fail(f"{exc.cmd[-1]} exited with {exc.returncode}")
    except subprocess.TimeoutExpired as exc:
        _fail(f"timed out: {exc}")
    except KeyboardInterrupt:
        # The children share our process group, so they got the SIGINT too
        # and are shutting down on their own.
        return 128 + signal.SIGINT
    return cli.EXIT_FAILURE


def _run(settings: Settings) -> int:
    admin = pika.BlockingConnection(
        broker.connection_parameters(settings, "demo")
    )
    try:
        channel = admin.channel()
        _reset(channel, settings)
        _say(
            f"1. reset: deleted {settings.exchange}, {settings.queue} and"
            " their dead-letter exchange and queue"
        )

        published = _run_publisher()
        _say(
            "2. publisher, started before any consumer or queue existed:"
            f" {len(published)} confirmed"
        )

        channel.confirm_delivery()
        channel.basic_publish(
            settings.exchange,
            settings.routing_key,
            b"this is not JSON",
            pika.BasicProperties(content_type="application/json"),
            mandatory=True,
        )
        _say("3. published 1 malformed message")

        processed, consumer_exit = _run_consumer_until_drained(
            channel, settings, set(published)
        )
        _say(
            f"4. consumer: processed {len(processed)}, then stopped by"
            f" SIGTERM (exit {consumer_exit})"
        )
        queue_depth = _depth(channel, settings.queue)
        dlq_depth = _depth(channel, settings.dead_letter_queue)
    finally:
        broker.close_quietly(admin)

    delivered = len(set(published) & set(processed))
    checks = [
        (
            "every confirmed message was processed"
            f" ({delivered}/{len(published)})",
            delivered == len(published),
        ),
        (
            "no message was processed twice",
            len(processed) == len(set(processed)),
        ),
        (f"main queue is empty ({queue_depth})", queue_depth == 0),
        (
            f"the malformed message is in the dead-letter queue ({dlq_depth})",
            dlq_depth == 1,
        ),
        (
            "the consumer shut down cleanly on SIGTERM",
            consumer_exit == 128 + signal.SIGTERM,
        ),
    ]
    _say("checks:")
    for label, ok in checks:
        _say(f"  {'ok  ' if ok else 'FAIL'} {label}")
    return cli.EXIT_OK if all(ok for _, ok in checks) else cli.EXIT_FAILURE


def _reset(channel: Any, settings: Settings) -> None:
    for queue in (settings.queue, settings.dead_letter_queue):
        channel.queue_delete(queue)
    for exchange in (settings.exchange, settings.dead_letter_exchange):
        channel.exchange_delete(exchange)


def _run_publisher() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pubsub.publisher"],
        stdout=subprocess.PIPE,
        text=True,
        timeout=STEP_TIMEOUT_S,
        check=True,
    )
    return result.stdout.split()


def _run_consumer_until_drained(
    channel: Any, settings: Settings, expected: set[str]
) -> tuple[list[str], int]:
    consumer = subprocess.Popen(
        [sys.executable, "-m", "pubsub.consumer"],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PUBSUB_MAX_MESSAGES": "0"},
    )
    processed: list[str] = []
    reader = threading.Thread(
        target=_collect_lines, args=(consumer, processed), daemon=True
    )
    reader.start()
    try:
        deadline = time.monotonic() + STEP_TIMEOUT_S
        while consumer.poll() is None and time.monotonic() < deadline:
            drained = expected <= set(processed)
            if drained and _depth(channel, settings.dead_letter_queue) >= 1:
                break
            time.sleep(_POLL_S)
    finally:
        exit_code = _stop(consumer)
    reader.join(timeout=STEP_TIMEOUT_S)
    return processed, exit_code


def _stop(process: subprocess.Popen[str]) -> int:
    """Asks a child to shut down gracefully, killing it only if it hangs."""
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            return process.wait(timeout=STEP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.wait()


def _collect_lines(process: subprocess.Popen[str], sink: list[str]) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        sink.append(line.strip())


def _depth(channel: Any, queue: str) -> int:
    declared = channel.queue_declare(queue, passive=True)
    return int(declared.method.message_count)


def _say(text: str) -> None:
    sys.stdout.write(f"{text}\n")


def _fail(reason: str) -> None:
    sys.stderr.write(f"pubsub.demo: {reason}\n")


if __name__ == "__main__":
    sys.exit(main())
