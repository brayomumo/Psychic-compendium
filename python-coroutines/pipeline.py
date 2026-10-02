"""A push pipeline built from coroutines: source -> stages -> sink.

Every stage owns its downstream ``target`` and closes it in ``finally``. One
rule covers every way a stage can stop:

* ``close()`` from upstream (normal end of input) cascades down the chain.
* An exception in a stage closes everything downstream, then propagates up.
* A finished downstream (``StopIteration`` on send) ends the stage, which in
  turn ends its upstream.

The demo parses tokens framed as ``<length> <token>...`` (the way data arrives
from a socket), joins each frame into a phrase, drops empty phrases, and
batches the rest.
"""

import logging
from collections.abc import Callable, Generator, Iterable, Sequence
from typing import TypeVar

from prime import Sink, coroutine, forward

logger = logging.getLogger(__name__)

T = TypeVar("T")
U = TypeVar("U")


class FrameError(ValueError):
    """The token stream is not a valid sequence of length-prefixed frames."""


class Flush(Exception):  # noqa: N818 - a control signal for throw(), not an error
    """Thrown into :func:`batch` to emit the partial batch now."""


def read_frame() -> Generator[None, str, tuple[str, ...]]:
    """Consume one ``<length> <token>*length`` frame and *return* its payload.

    This is a subgenerator for ``yield from``, which primes it by calling
    ``next()``. Do not prime it yourself: that ``next()`` would then resume it
    with ``None`` as the header, and ``int(None)`` raises ``TypeError``.

    Returns:
        The frame's tokens.

    Raises:
        FrameError: The header is not a non-negative integer.
    """
    header = yield
    try:
        length = int(header)
    except ValueError as exc:
        raise FrameError(
            f"frame header must be an integer: {header!r}"
        ) from exc
    if length < 0:
        raise FrameError(f"frame length must be >= 0, got {length}")

    payload: list[str] = []
    try:
        while len(payload) < length:
            payload.append((yield))
    except GeneratorExit:
        logger.warning(
            "dropping truncated frame: got %d of %d tokens",
            len(payload),
            length,
        )
        raise
    return tuple(payload)


@coroutine
def deframe(target: Sink[tuple[str, ...]]) -> Generator[None, str, None]:
    """Turn tokens into frames by delegating to :func:`read_frame`.

    While delegating, every ``send``, ``throw`` and ``close`` on this stage
    goes straight to the subgenerator, and the subgenerator's ``return`` value
    becomes the value of the ``yield from`` expression. The parser reads as
    straight-line code instead of a hand-written state machine.

    Args:
        target: Receives each complete frame.
    """
    try:
        while True:
            frame = yield from read_frame()
            if not forward(target, frame):
                return
    finally:
        target.close()


@coroutine
def transform(
    func: Callable[[T], U], target: Sink[U]
) -> Generator[None, T, None]:
    """Send ``func(item)`` downstream for every item received.

    Args:
        func: Applied to each item.
        target: Receives the results.
    """
    try:
        while True:
            item = yield
            if not forward(target, func(item)):
                return
    finally:
        target.close()


@coroutine
def select(
    predicate: Callable[[T], bool], target: Sink[T]
) -> Generator[None, T, None]:
    """Forward only the items for which ``predicate`` is true.

    Args:
        predicate: Decides whether an item passes.
        target: Receives the items that pass.
    """
    try:
        while True:
            item = yield
            if predicate(item) and not forward(target, item):
                return
    finally:
        target.close()


@coroutine
def take(limit: int, target: Sink[T]) -> Generator[None, T, None]:
    """Forward the first ``limit`` items, then finish so upstream stops.

    The stage finishes as soon as it forwards item number ``limit``, so the
    sender sees ``StopIteration`` on that send and pulls nothing more. With
    ``limit == 0`` one item is consumed and dropped: a primed coroutine must
    accept one ``send()`` before it can refuse.

    Args:
        limit: Number of items to forward.
        target: Receives the items.

    Raises:
        ValueError: ``limit`` is negative.
    """
    try:
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        remaining = limit
        while True:
            item = yield
            if remaining == 0:
                return
            remaining -= 1
            if not forward(target, item) or remaining == 0:
                return
    finally:
        target.close()


@coroutine
def batch(size: int, target: Sink[list[T]]) -> Generator[None, T, None]:
    """Group items into lists of ``size``.

    * ``throw(Flush())`` emits the partial batch now and keeps running.
      ``throw()`` returns normally because the coroutine handled the exception
      and reached its next ``yield``.
    * ``close()`` emits the partial batch before shutting down, so the end of
      the stream is not lost.
    * Any other thrown exception is unhandled: it ends this stage, ``finally``
      closes the downstream, and the exception propagates out of ``throw()``.

    Args:
        size: Items per batch.
        target: Receives each batch.

    Raises:
        ValueError: ``size`` is less than 1.
    """
    pending: list[T] = []
    try:
        if size < 1:
            raise ValueError(f"batch size must be >= 1, got {size}")
        while True:
            try:
                pending.append((yield))
            except Flush:
                pass
            else:
                if len(pending) < size:
                    continue
            if pending:
                ready, pending = pending, []
                if not forward(target, ready):
                    return
    except GeneratorExit:
        if pending:
            forward(target, pending)
        raise
    finally:
        target.close()


@coroutine
def collect(into: list[T]) -> Generator[None, T, None]:
    """Sink: append every item received to ``into``.

    Args:
        into: The list to append to.
    """
    while True:
        into.append((yield))


def feed(items: Iterable[T], target: Sink[T]) -> None:
    """Send every item into a pipeline, then close its head.

    Pulling from ``items`` stops as soon as the pipeline finishes early (for
    example a :func:`take` stage reached its limit), so ``items`` may be
    infinite.

    Args:
        items: The source.
        target: The first stage of the pipeline.
    """
    try:
        for item in items:
            if not forward(target, item):
                break
    finally:
        target.close()


def phrase_pipeline(batch_size: int, into: list[list[str]]) -> Sink[str]:
    """Build tokens -> frames -> phrases -> non-empty -> batches -> ``into``.

    Args:
        batch_size: Phrases per batch.
        into: Receives the batches.

    Returns:
        The head of the pipeline, ready for :func:`feed`.
    """
    return deframe(
        transform(" ".join, select(bool, batch(batch_size, collect(into))))
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the phrase pipeline on tokens whose last frame is truncated.

    Args:
        argv: Unused; present so every demo has the same entry point.

    Returns:
        The process exit code.
    """
    del argv
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    tokens = "3 the quick fox 0 2 jumps over 1 the 2 lazy dog 3 cut short"
    batches: list[list[str]] = []
    feed(tokens.split(" "), phrase_pipeline(batch_size=2, into=batches))
    print(f"tokens:  {tokens}")
    for number, group in enumerate(batches, start=1):
        print(f"batch {number}: {group}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
