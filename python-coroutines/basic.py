"""The methods that turn a generator into a coroutine: send, close, throw.

Run ``python3 basic.py`` for a narrated tour.
"""

import inspect
from collections.abc import Callable, Generator, Sequence

from prime import coroutine


@coroutine
def grep(
    pattern: str, on_match: Callable[[str], object]
) -> Generator[None, str, None]:
    """Sink coroutine: report every line sent in that contains ``pattern``.

    The bare ``yield`` produces ``None``, so ``send()`` returns ``None``: data
    flows one way, into the coroutine.

    Args:
        pattern: Substring to look for.
        on_match: Called with each matching line.
    """
    while True:
        line = yield
        if pattern in line:
            on_match(line)


@coroutine
def running_average() -> Generator[float, float, None]:
    """Request/response coroutine: ``send(x)`` returns the mean so far.

    ``yield average`` hands a value back to the caller *and* waits for the next
    input, so ``send()`` behaves like a function call that keeps its state.
    """
    total = 0.0
    count = 0
    average = 0.0
    while True:
        value = yield average
        total += value
        count += 1
        average = total / count


def main(argv: Sequence[str] | None = None) -> int:
    """Narrate priming, send() return values, generator states, close, throw.

    Args:
        argv: Unused; present so every demo has the same entry point.

    Returns:
        The process exit code.
    """
    del argv

    def raw() -> Generator[None, str, None]:
        while True:
            yield

    unprimed = raw()
    print(f"new generator:          {inspect.getgeneratorstate(unprimed)}")
    try:
        unprimed.send("hello")
    except TypeError as exc:
        print(f"send() before priming:  TypeError: {exc}")
    unprimed.close()

    matches: list[str] = []
    finder = grep("brian", matches.append)
    print(f"primed grep:            {inspect.getgeneratorstate(finder)}")
    returned = {
        finder.send(f"line {i} {'brian' if i % 2 else 'nobody'}")
        for i in range(6)
    }
    print(f"grep matched:           {matches}")
    print(f"grep send() returned:   {returned}")

    averager = running_average()
    means = [averager.send(x) for x in (10.0, 20.0, 60.0)]
    print(f"average send() returned {means}")

    finder.close()
    print(f"grep after close():     {inspect.getgeneratorstate(finder)}")
    try:
        finder.send("brian again")
    except StopIteration:
        print("send() after close():   StopIteration, it never runs again")

    try:
        averager.throw(ValueError("injected"))
    except ValueError as exc:
        print(f"unhandled throw():      re-raised to the caller: {exc!r}")
    print(f"average after throw():  {inspect.getgeneratorstate(averager)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
