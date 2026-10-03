"""Walks through every RPC pattern and error path against a running server.

Usage: python -m productclient.demo [--target HOST:PORT] [--timeout S]
"""

import argparse
import logging
import math
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import TextIO

from productclient.client import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    AlreadyExistsError,
    CatalogClient,
    CatalogError,
    DeadlineExceededError,
    InvalidArgumentError,
    NewProduct,
    NotFoundError,
    Product,
    UnavailableError,
)

__all__ = ["main"]

logger = logging.getLogger("productclient.demo")

EXIT_OK = 0
EXIT_FAILURE = 1


class DemoCheckError(RuntimeError):
    """The server answered, but not as the demo expected."""


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT_S:
        raise argparse.ArgumentTypeError(
            f"must be in (0, {MAX_TIMEOUT_S:g}], got {text}"
        )
    return value


def _check(condition: bool, what: str) -> None:
    # Not `assert`: asserts vanish under `python -O`, and the first version
    # relied on one for control flow.
    if not condition:
        raise DemoCheckError(what)


Say = Callable[[str], None]


def walkthrough(client: CatalogClient, out: TextIO) -> None:
    """Exercises every RPC kind and error path, printing what happens.

    Args:
        client: A connected client.
        out: Where to print the walkthrough.

    Raises:
        DemoCheckError: A response was not what the server's contract says.
        CatalogError: An RPC failed unexpectedly.
    """
    run = uuid.uuid4().hex[:6]  # unique names, so the demo can rerun

    def say(line: str) -> None:
        print(line, file=out, flush=True)

    kettle = _unary(client, say, run)
    _errors(client, say, run)
    _server_streaming(client, say, run)
    _client_streaming(client, say, run)
    _bidirectional(client, say, kettle)
    _deadline(client, say, kettle)


def _unary(client: CatalogClient, say: Say, run: str) -> Product:
    say("1. Unary: AddProduct with an idempotency key, then retry it")
    key = f"demo-{run}"
    kettle_spec = NewProduct(f"Kettle {run}", "1.7 l, boils fast", 2500)
    kettle = client.add_product(kettle_spec, idempotency_key=key)
    retry = client.add_product(kettle_spec, idempotency_key=key)
    _check(retry.id == kettle.id, "a retry with the same key made a duplicate")
    say(f"   added {kettle.name!r} as {kettle.id}; retry returned the same id")
    teapot = client.add_product(
        NewProduct(f"Teapot {run}", f"goes with Kettle {run}")
    )
    say(f"   added {teapot.name!r} as {teapot.id}")
    say("2. Unary: GetProduct")
    _check(client.get_product(kettle.id) == kettle, "GetProduct mismatch")
    say(f"   got {kettle.id}: {kettle.price_cents} cents")
    return kettle


def _errors(client: CatalogClient, say: Say, run: str) -> None:
    say("3. Errors carry structured details")
    try:
        client.get_product("no-such-id")
    except NotFoundError as e:
        say(f"   NOT_FOUND for resource {e.resource_name!r}")
    else:
        raise DemoCheckError("a missing product was found")
    try:
        client.add_product(NewProduct(" ", price_cents=-1))
    except InvalidArgumentError as e:
        fields = ", ".join(f"{v.field} ({v.description})" for v in e.violations)
        say(f"   INVALID_ARGUMENT: {fields}")
    else:
        raise DemoCheckError("an invalid product was accepted")
    try:
        client.add_product(NewProduct(f"KETTLE {run}"))
    except AlreadyExistsError as e:
        say(f"   ALREADY_EXISTS for {e.resource_name!r}")
    else:
        raise DemoCheckError("a duplicate name was accepted")


def _server_streaming(client: CatalogClient, say: Say, run: str) -> None:
    say("4. Server streaming: SearchProducts matches name or description")
    for p in client.search(f"kettle {run}"):
        say(f"   - {p.name}")


def _client_streaming(client: CatalogClient, say: Say, run: str) -> None:
    say("5. Client streaming: BulkAddProducts (all or nothing)")
    batch = [NewProduct(f"{n} {run}", price_cents=100) for n in ("Mug", "Cup")]
    added = client.bulk_add(batch)
    say(f"   stored {[p.name for p in added]}")
    try:
        client.bulk_add([NewProduct(f"Spoon {run}"), NewProduct("")])
    except InvalidArgumentError as e:
        say(f"   rejected whole batch: {e.violations[0].field}")
    else:
        raise DemoCheckError("a batch with an invalid product was accepted")
    _check(
        not any(True for _ in client.search(f"spoon {run}")),
        "a rejected batch stored something",
    )


def _bidirectional(client: CatalogClient, say: Say, kettle: Product) -> None:
    say("6. Bidirectional streaming: QuoteProducts")
    items = [(kettle.id, 2), ("no-such-id", 1), (kettle.id, 0)]
    for q in client.quote(items):
        total = "" if q.total_cents is None else f" = {q.total_cents} cents"
        say(f"   {q.quantity} x {q.product_id[:8]}: {q.status.name}{total}")


def _deadline(client: CatalogClient, say: Say, kettle: Product) -> None:
    say("7. Deadlines: a 100 ms stream whose client takes 300 ms to send")

    def slow_items() -> Iterator[tuple[str, int]]:
        time.sleep(0.3)  # simulated slow work, never synchronization
        yield (kettle.id, 1)

    try:
        list(client.quote(slow_items(), timeout=0.1))
    except DeadlineExceededError as e:
        say(f"   DEADLINE_EXCEEDED (request {e.request_id})")
    else:
        raise DemoCheckError("the stream outlived its deadline")


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the walkthrough.

    Args:
        argv: Command-line arguments; None means sys.argv[1:].

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        default="127.0.0.1:50059",
        help="server address (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=DEFAULT_TIMEOUT_S,
        metavar="S",
        help="per-call deadline in seconds (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(message)s"
    )
    try:
        with CatalogClient(args.target, timeout=args.timeout) as client:
            walkthrough(client, sys.stdout)
    except UnavailableError as e:
        logger.error("cannot reach %s: %s", args.target, e.message)
        return EXIT_FAILURE
    except (CatalogError, DemoCheckError) as e:
        logger.error("demo failed: %s", e)
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
