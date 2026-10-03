"""Topic-based publish/subscribe where every subscriber is a coroutine.

Delivery is synchronous and in-process: ``publish()`` returns once every
subscriber of the topic has handled the message. The broker owns its
subscribers. A subscriber that raises or finishes is removed, so one bad
subscriber cannot take the others down. ``close()`` shuts every subscriber
down exactly once.
"""

import inspect
import logging
import random
from collections import deque
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from prime import Sink, coroutine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    """A published message, carrying its topic for multi-topic subscribers."""

    topic: str
    payload: object


class BrokerClosedError(RuntimeError):
    """The broker was used after ``close()``."""


class BrokerOverflowError(RuntimeError):
    """Subscribers published too many messages during a single delivery."""


class Broker:
    """Deliver published messages to the coroutines subscribed to each topic.

    Use it as a context manager, or call :meth:`close` when done.
    """

    def __init__(self, max_cascade: int = 1000) -> None:
        """Create an open broker with no subscribers.

        Args:
            max_cascade: Most messages subscribers may publish while one
                top-level ``publish()`` is being delivered. This bounds both
                the pending queue and the total work one publish can cause.

        Raises:
            ValueError: ``max_cascade`` is less than 1.
        """
        if max_cascade < 1:
            raise ValueError(f"max_cascade must be >= 1, got {max_cascade}")
        self._topics: dict[str, list[Sink[Message]]] = {}
        self._pending: deque[Message] = deque()
        self._max_cascade = max_cascade
        self._cascade = 0
        self._dispatching = False
        self._closed = False

    def __enter__(self) -> Self:
        """Return the broker itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the broker, whether or not the block raised."""
        self.close()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has run."""
        return self._closed

    def subscribe(self, topic: str, subscriber: Sink[Message]) -> None:
        """Deliver future messages on ``topic`` to ``subscriber``.

        The broker takes ownership and closes the subscriber when it is
        unsubscribed from its last topic or the broker closes. Checking that
        it is primed here turns the classic forgot-to-prime mistake into an
        immediate error instead of a failed delivery later.

        Args:
            topic: Non-empty topic name.
            subscriber: A primed coroutine suspended at a ``yield``.

        Raises:
            BrokerClosedError: The broker is closed.
            ValueError: Empty topic, unprimed or finished subscriber, or a
                duplicate subscription.
        """
        self._ensure_open()
        if not topic:
            raise ValueError("topic must be a non-empty string")
        state = inspect.getgeneratorstate(subscriber)
        if state != inspect.GEN_SUSPENDED:
            raise ValueError(
                f"subscriber must be suspended at a yield, but it is {state}; "
                "decorate its generator function with @coroutine"
            )
        subscribers = self._topics.setdefault(topic, [])
        if subscriber in subscribers:
            raise ValueError(f"{subscriber!r} already subscribed to {topic!r}")
        subscribers.append(subscriber)

    def unsubscribe(self, topic: str, subscriber: Sink[Message]) -> None:
        """Stop delivering ``topic`` to ``subscriber``.

        The subscriber is closed once it has no topics left.

        Args:
            topic: The topic to leave.
            subscriber: A subscriber of ``topic``.

        Raises:
            ValueError: ``subscriber`` is not subscribed to ``topic``.
            RuntimeError: Called by the subscriber on itself during delivery.
        """
        subscribers = self._topics.get(topic, [])
        if subscriber not in subscribers:
            raise ValueError(f"{subscriber!r} is not subscribed to {topic!r}")
        if inspect.getgeneratorstate(subscriber) == inspect.GEN_RUNNING:
            # close() on a running generator: "generator already executing".
            raise RuntimeError(
                "a subscriber cannot unsubscribe itself while handling a "
                "message; return from the generator instead"
            )
        subscribers.remove(subscriber)
        if not subscribers:
            del self._topics[topic]
        if not self._subscribed_anywhere(subscriber):
            subscriber.close()

    def publish(self, topic: str, payload: object) -> None:
        """Deliver ``payload`` to every subscriber of ``topic``, in order.

        A topic with no subscribers drops the message. A subscriber that
        publishes while handling a message must not recurse into generators
        that are still running, so that message is queued and delivered after
        the current one. A subscriber stuck in a republish loop would cycle
        forever even with a one-slot queue, so the bound counts *every*
        re-entrant publish; past ``max_cascade`` the publisher gets
        ``BrokerOverflowError`` and, being a subscriber, is removed.

        Args:
            topic: Topic to publish on.
            payload: Any object; it is passed by reference, not copied.

        Raises:
            BrokerClosedError: The broker is closed.
            BrokerOverflowError: Called during delivery after ``max_cascade``
                re-entrant publishes.
        """
        self._ensure_open()
        if self._dispatching:
            if self._cascade >= self._max_cascade:
                raise BrokerOverflowError(
                    f"subscribers published {self._cascade} messages during "
                    "one delivery; is one republishing in a loop?"
                )
            self._cascade += 1
            self._pending.append(Message(topic, payload))
            return
        self._pending.append(Message(topic, payload))
        self._dispatching = True
        try:
            while self._pending:
                self._deliver(self._pending.popleft())
        finally:
            if self._pending:
                # Only reachable when delivery was aborted (e.g. Ctrl+C).
                # Delivering these later, inside an unrelated publish, would
                # be more surprising than dropping them loudly.
                logger.warning(
                    "delivery aborted; dropping %d queued messages",
                    len(self._pending),
                )
                self._pending.clear()
            self._dispatching = False
            self._cascade = 0

    def close(self) -> None:
        """Close every subscriber exactly once. Calling it again does nothing.

        A subscriber whose cleanup raises is logged and does not prevent the
        others from being closed.

        Raises:
            RuntimeError: Called from inside a subscriber during delivery.
        """
        if self._closed:
            return
        if self._dispatching:
            raise RuntimeError(
                "cannot close the broker from inside a subscriber"
            )
        self._closed = True
        subscribers = {id(s): s for subs in self._topics.values() for s in subs}
        self._topics.clear()
        for subscriber in subscribers.values():
            try:
                subscriber.close()
            except Exception:
                logger.exception(
                    "subscriber %r raised while closing", subscriber
                )

    def _deliver(self, message: Message) -> None:
        for subscriber in tuple(self._topics.get(message.topic, ())):
            if subscriber not in self._topics.get(message.topic, ()):
                continue  # an earlier subscriber unsubscribed it just now
            try:
                subscriber.send(message)
            except StopIteration:
                logger.info("subscriber %r finished; removing it", subscriber)
                self._remove_everywhere(subscriber)
            except Exception:
                logger.exception(
                    "subscriber %r raised; removing it", subscriber
                )
                self._remove_everywhere(subscriber)

    def _subscribed_anywhere(self, subscriber: Sink[Message]) -> bool:
        return any(subscriber in subs for subs in self._topics.values())

    def _remove_everywhere(self, subscriber: Sink[Message]) -> None:
        for topic in list(self._topics):
            subs = self._topics[topic]
            if subscriber in subs:
                subs.remove(subscriber)
                if not subs:
                    del self._topics[topic]

    def _ensure_open(self) -> None:
        if self._closed:
            raise BrokerClosedError("broker is closed")


# --- Demo subscribers and producer ------------------------------------------

READING_RANGES = {"temperature": (15.0, 35.0), "humidity": (20.0, 80.0)}


@coroutine
def display(name: str) -> Generator[None, Message, None]:
    """Print every message received.

    Args:
        name: Label printed with each message.
    """
    while True:
        message = yield
        print(f"  {name:<9} {message.topic:<11} {message.payload}")


@coroutine
def mean_tracker(name: str) -> Generator[None, Message, None]:
    """Track a running mean and print it when closed.

    Args:
        name: Label printed with the summary.

    Raises:
        TypeError: A payload is not a number.
    """
    count = 0
    total = 0.0
    try:
        while True:
            message = yield
            if not isinstance(message.payload, int | float):
                raise TypeError(f"expected a number, got {message.payload!r}")
            count += 1
            total += message.payload
    finally:
        mean = f"{total / count:.1f}" if count else "n/a"
        print(f"  {name} closed: {count} readings, mean {mean}")


@coroutine
def first(limit: int) -> Generator[None, Message, None]:
    """Handle ``limit`` messages, then return, which ends the subscription.

    Args:
        limit: Number of messages to handle, at least 1.
    """
    for _ in range(limit):
        message = yield
        print(f"  first-{limit:<3} {message.topic:<11} {message.payload}")


@coroutine
def buggy(fail_on: int) -> Generator[None, Message, None]:
    """Raise on message number ``fail_on`` to show the broker isolating it.

    Args:
        fail_on: The 1-based message number to fail on.

    Raises:
        RuntimeError: On message number ``fail_on``.
    """
    received = 0
    while True:
        yield
        received += 1
        if received == fail_on:
            raise RuntimeError(f"simulated bug on message {received}")


def sensor_readings(count: int, seed: int) -> Iterator[tuple[str, float]]:
    """Yield a bounded, reproducible stream of ``(topic, value)`` readings.

    Args:
        count: Number of readings.
        seed: Seed for the random generator.

    Yields:
        ``(topic, value)`` pairs.

    Raises:
        ValueError: ``count`` is negative.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    rng = random.Random(seed)
    topics = sorted(READING_RANGES)
    for _ in range(count):
        topic = rng.choice(topics)
        low, high = READING_RANGES[topic]
        yield topic, round(rng.uniform(low, high), 1)


def main(argv: Sequence[str] | None = None) -> int:
    """Publish a short, seeded stream of sensor readings to a few subscribers.

    Args:
        argv: Unused; present so every demo has the same entry point.

    Returns:
        The process exit code.
    """
    del argv
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    with Broker() as broker:
        broker.subscribe("temperature", display("display"))
        broker.subscribe("humidity", display("display"))
        broker.subscribe("temperature", mean_tracker("temp-mean"))
        broker.subscribe("humidity", first(2))
        broker.subscribe("temperature", buggy(fail_on=2))
        for topic, value in sensor_readings(count=8, seed=7):
            broker.publish(topic, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
