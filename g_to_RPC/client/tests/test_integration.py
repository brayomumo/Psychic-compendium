"""End-to-end tests: the Python client against the real Go server.

They skip cleanly when the server binary has not been built.
"""

import contextlib
import io
import socket
import subprocess
import sys
import time
import unittest
import uuid
from collections.abc import Iterator

import grpc

from ecommerce.v1 import product_pb2, product_pb2_grpc
from procs import BINARY, SKIP_REASON, STOPPED_BY_SIGTERM, RunningServer
from productclient import demo
from productclient.client import (
    REQUEST_ID_KEY,
    AlreadyExistsError,
    CatalogClient,
    DeadlineExceededError,
    FailedPreconditionError,
    InvalidArgumentError,
    NewProduct,
    NotFoundError,
    QuoteStatus,
    ResourceExhaustedError,
    UnavailableError,
)


def unique(name: str) -> str:
    return f"{name} {uuid.uuid4().hex[:8]}"


@unittest.skipUnless(BINARY.is_file(), SKIP_REASON)
class EndToEndTest(unittest.TestCase):
    server: RunningServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = RunningServer("-max-products", "1000").__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.__exit__(None, None, None)
        # A clean, signal-driven graceful stop exits 128 + SIGTERM.
        if cls.server.exit_code != STOPPED_BY_SIGTERM:
            raise AssertionError(
                f"server exit code {cls.server.exit_code}, "
                f"want {STOPPED_BY_SIGTERM}"
            )

    def setUp(self) -> None:
        self.client = CatalogClient(self.server.address, timeout=5)
        self.addCleanup(self.client.close)

    def test_unary_add_and_get(self) -> None:
        added = self.client.add_product(NewProduct(unique("Kettle"), "x", 2500))
        self.assertEqual(self.client.get_product(added.id), added)

    def test_idempotency_key_makes_retries_safe(self) -> None:
        name, key = unique("Kettle"), str(uuid.uuid4())
        first = self.client.add_product(NewProduct(name), idempotency_key=key)
        again = self.client.add_product(NewProduct(name), idempotency_key=key)
        self.assertEqual(first.id, again.id)
        with self.assertRaises(FailedPreconditionError):
            self.client.add_product(
                NewProduct(unique("Other")), idempotency_key=key
            )

    def test_structured_errors_cross_the_language_boundary(self) -> None:
        with self.assertRaises(NotFoundError) as nf:
            self.client.get_product("missing-id")
        self.assertEqual(nf.exception.resource_name, "missing-id")

        with self.assertRaises(InvalidArgumentError) as bad:
            self.client.add_product(NewProduct("", price_cents=-5))
        self.assertEqual(
            [v.field for v in bad.exception.violations],
            ["product.name", "product.price_cents"],
        )

        name = unique("Dup")
        self.client.add_product(NewProduct(name))
        with self.assertRaises(AlreadyExistsError) as dup:
            self.client.add_product(NewProduct(name.upper()))
        self.assertEqual(dup.exception.resource_name, name.upper())

    def test_server_streaming_search(self) -> None:
        tag = uuid.uuid4().hex[:8]
        for name in ("b", "a", "c"):
            self.client.add_product(NewProduct(f"{name}-{tag}"))
        names = [p.name for p in self.client.search(tag)]
        self.assertEqual(names, [f"a-{tag}", f"b-{tag}", f"c-{tag}"])

    def test_client_streaming_bulk_add_is_atomic(self) -> None:
        tag = uuid.uuid4().hex[:8]
        stored = self.client.bulk_add(
            NewProduct(f"{n}-{tag}") for n in ("x", "y")
        )
        self.assertEqual([p.name for p in stored], [f"x-{tag}", f"y-{tag}"])
        with self.assertRaises(InvalidArgumentError) as ctx:
            self.client.bulk_add(
                [NewProduct(f"z-{tag}"), NewProduct(""), NewProduct(f"w-{tag}")]
            )
        self.assertEqual(ctx.exception.violations[0].field, "products[1].name")
        self.assertEqual(list(self.client.search(f"z-{tag}")), [])

    def test_bidirectional_quotes(self) -> None:
        p = self.client.add_product(NewProduct(unique("Mug"), price_cents=300))
        quotes = list(self.client.quote([(p.id, 3), ("nope", 1), (p.id, 0)]))
        self.assertEqual(
            [(q.status, q.total_cents) for q in quotes],
            [
                (QuoteStatus.OK, 900),
                (QuoteStatus.NOT_FOUND, None),
                (QuoteStatus.INVALID_QUANTITY, None),
            ],
        )

    def test_quotes_answer_before_the_stream_ends(self) -> None:
        # Bidirectional: the first answer arrives while the client is still
        # holding back the second item.
        p = self.client.add_product(NewProduct(unique("Cup"), price_cents=1))
        answered: list[int] = []

        def items() -> Iterator[tuple[str, int]]:
            yield (p.id, 1)
            deadline = time.monotonic() + 5
            while not answered and time.monotonic() < deadline:
                time.sleep(0.01)  # observe only; the assertion below decides
            yield (p.id, 2)

        for q in self.client.quote(items()):
            answered.append(q.quantity)
        self.assertEqual(answered, [1, 2])

    def test_deadline_exceeded_mid_stream(self) -> None:
        def slow() -> Iterator[tuple[str, int]]:
            time.sleep(0.5)  # simulated slow client
            yield ("x", 1)

        start = time.monotonic()
        with self.assertRaises(DeadlineExceededError):
            list(self.client.quote(slow(), timeout=0.1))
        self.assertLess(time.monotonic() - start, 2)

    def test_oversized_request_is_resource_exhausted(self) -> None:
        with self.assertRaises(ResourceExhaustedError):
            self.client.add_product(NewProduct("big", "x" * (2 << 20)))

    def test_request_id_round_trips_in_metadata(self) -> None:
        with grpc.insecure_channel(self.server.address) as channel:
            stub = product_pb2_grpc.ProductCatalogServiceStub(channel)
            # Streaming calls expose response headers via initial_metadata().
            stream = stub.SearchProducts(
                product_pb2.SearchProductsRequest(),
                timeout=5,
                metadata=((REQUEST_ID_KEY, "trace-me"),),
            )
            headers = dict(stream.initial_metadata())
            list(stream)
        self.assertEqual(headers.get(REQUEST_ID_KEY), "trace-me")

    def test_demo_walkthrough_passes(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = demo.main(["--target", self.server.address])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("DEADLINE_EXCEEDED", out.getvalue())


class UnreachableServerTest(unittest.TestCase):
    def test_unreachable_server_fails_fast_instead_of_hanging(self) -> None:
        with socket.socket() as s:  # find a port nobody listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        start = time.monotonic()
        with (
            CatalogClient(f"127.0.0.1:{port}", timeout=2) as client,
            self.assertRaises((UnavailableError, DeadlineExceededError)),
        ):
            client.get_product("x")
        self.assertLess(time.monotonic() - start, 5)


@unittest.skipUnless(BINARY.is_file(), SKIP_REASON)
class LauncherTest(unittest.TestCase):
    def test_make_run_launcher_exits_zero_and_stops_server_cleanly(
        self,
    ) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "productclient.launch",
                "--server",
                str(BINARY),
                "--port",
                "0",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"server exited with {STOPPED_BY_SIGTERM}", result.stderr)

    def test_launcher_reports_missing_binary(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "productclient.launch", "--server", "/nope"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("make build", result.stderr)


if __name__ == "__main__":
    unittest.main()
