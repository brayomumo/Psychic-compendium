"""Shared helpers for push-style (``send``-driven) generator coroutines."""

import functools
from collections.abc import Callable, Generator
from typing import ParamSpec, TypeAlias, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
Y = TypeVar("Y")
S = TypeVar("S")
R = TypeVar("R")

Sink: TypeAlias = Generator[None, T, None]
"""A coroutine that consumes ``T`` values via ``send()`` and yields nothing."""


def coroutine(
    func: Callable[P, Generator[Y, S, R]],
) -> Callable[P, Generator[Y, S, R]]:
    """Make a generator function return generators already at their first yield.

    A new generator has not run any of its body, so ``send(value)`` with
    anything but ``None`` raises ``TypeError``. Priming runs the body up to the
    first ``yield``. Argument checks placed before that ``yield`` therefore run
    at construction time instead of on the first ``send()``. The value produced
    by the first ``yield`` is discarded.

    Args:
        func: A generator function.

    Returns:
        A function with the same signature that returns primed generators.
    """

    @functools.wraps(func)
    def primed(*args: P.args, **kwargs: P.kwargs) -> Generator[Y, S, R]:
        gen = func(*args, **kwargs)
        try:
            next(gen)
        except StopIteration as exc:
            # In a generator, PEP 479 would turn this into an opaque
            # RuntimeError. This is a plain function, so name the mistake.
            raise RuntimeError(
                f"{func.__qualname__}() returned before its first yield, so it "
                "can never receive values"
            ) from exc
        return gen

    return primed


def forward(target: Sink[T], item: T) -> bool:
    """Send ``item`` to ``target`` unless ``target`` has already finished.

    A finished generator answers ``send()`` with ``StopIteration``. That
    exception must not escape a generator (PEP 479 turns it into
    ``RuntimeError``), so pipeline stages use this helper and stop cleanly
    once their downstream has stopped, the way ``head`` ends a Unix pipe.

    Args:
        target: The coroutine to send to.
        item: The value to send.

    Returns:
        ``True`` if ``target`` accepted the item and is still running,
        ``False`` if it has finished.
    """
    try:
        target.send(item)
    except StopIteration:
        return False
    return True
