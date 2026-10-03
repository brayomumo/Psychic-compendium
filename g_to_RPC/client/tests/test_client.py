"""Client unit tests: no server, a fake stub that records every call."""

import ast
import json
import math
import pathlib
import unittest
from collections.abc import Iterator
from typing import Any

import grpc
from google.protobuf import any_pb2
from google.rpc import error_details_pb2, status_pb2

from ecommerce.v1 import product_pb2
from productclient import client
from productclient.client import (
    REQUEST_ID_KEY,
    CancelledError,
    CatalogClient,
    CatalogError,
    DeadlineExceededError,
    InvalidArgumentError,
    NewProduct,
    NotFoundError,
    QuoteStatus,
    UnavailableError,
    from_rpc_error,
)

PACKAGE_DIR = pathlib.Path(client.__file__).parent


class FakeRpcError(grpc.RpcError, grpc.Call):
    """An RpcError shaped like the ones grpc raises (they are also Calls)."""

    def __init__(
        self,
        code: grpc.StatusCode,
        message: str = "",
        details: list[Any] | None = None,
        time_remaining: float | None = 5.0,
    ) -> None:
        self._code, self._message = code, message
        self._time_remaining = time_remaining
        self._trailers: tuple[tuple[str, str | bytes], ...] = ()
        if details:
            st = status_pb2.Status(code=code.value[0], message=message)
            for d in details:
                packed = any_pb2.Any()
                packed.Pack(d)
                st.details.append(packed)
            self._trailers = (
                ("grpc-status-details-bin", st.SerializeToString()),
            )

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._message

    def trailing_metadata(self) -> Any:
        return self._trailers

    def initial_metadata(self) -> Any:
        return ()

    # grpc returns None when a call has no deadline; the stubs say float.
    def time_remaining(self) -> float | None:  # type: ignore[override]
        return self._time_remaining

    def is_active(self) -> bool:
        return False

    def cancel(self) -> bool:
        return False

    def add_callback(self, callback: Any) -> bool:
        return False


class FakeStream:
    """A response iterator that records cancel(), like a grpc stream call."""

    def __init__(self, responses: list[Any], error: Exception | None = None):
        self._responses = responses
        self._error = error
        self.cancelled = False

    def __iter__(self) -> Iterator[Any]:
        yield from self._responses
        if self._error is not None:
            raise self._error

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class FakeStub:
    """Stands in for ProductCatalogServiceStub and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.search_stream = FakeStream([])
        self.quote_stream = FakeStream([])
        self.error: Exception | None = None

    def _record(
        self, method: str, request: Any, kwargs: dict[str, Any]
    ) -> None:
        if method in {"BulkAddProducts", "QuoteProducts"}:
            request = list(request)  # drain the streamed requests
        self.calls.append((method, request, kwargs))
        if self.error is not None:
            raise self.error

    def AddProduct(self, request: Any, **kwargs: Any) -> Any:  # noqa: N802 - grpc stub name
        self._record("AddProduct", request, kwargs)
        return product_pb2.AddProductResponse(
            product=product_pb2.Product(id="id-1", name=request.product.name)
        )

    def GetProduct(self, request: Any, **kwargs: Any) -> Any:  # noqa: N802 - grpc stub name
        self._record("GetProduct", request, kwargs)
        return product_pb2.GetProductResponse(
            product=product_pb2.Product(id=request.id, name="Kettle")
        )

    def SearchProducts(self, request: Any, **kwargs: Any) -> Any:  # noqa: N802 - grpc stub name
        self._record("SearchProducts", request, kwargs)
        return self.search_stream

    def BulkAddProducts(self, requests: Any, **kwargs: Any) -> Any:  # noqa: N802 - grpc stub name
        self._record("BulkAddProducts", requests, kwargs)
        return product_pb2.BulkAddProductsResponse()

    def QuoteProducts(self, requests: Any, **kwargs: Any) -> Any:  # noqa: N802 - grpc stub name
        self._record("QuoteProducts", requests, kwargs)
        return self.quote_stream


def make_client(timeout: float = 2.5) -> tuple[CatalogClient, FakeStub]:
    c = CatalogClient("127.0.0.1:1", timeout=timeout)
    stub = FakeStub()
    c._stub = stub  # type: ignore[assignment]  # test double
    return c, stub


class DeadlineTest(unittest.TestCase):
    def test_rpc_without_deadline_bug_every_call_sets_a_timeout(self) -> None:
        c, stub = make_client(timeout=2.5)
        with c:
            c.add_product(NewProduct("Kettle"))
            c.get_product("id-1")
            list(c.search("k"))
            c.bulk_add([NewProduct("a")])
            list(c.quote([("id-1", 1)]))
        self.assertEqual(
            [name for name, _, _ in stub.calls],
            [
                "AddProduct",
                "GetProduct",
                "SearchProducts",
                "BulkAddProducts",
                "QuoteProducts",
            ],
        )
        for name, _, kwargs in stub.calls:
            with self.subTest(method=name):
                self.assertEqual(kwargs.get("timeout"), 2.5)

    def test_per_call_timeout_overrides_default(self) -> None:
        c, stub = make_client(timeout=2.5)
        c.get_product("id-1", timeout=0.25)
        self.assertEqual(stub.calls[0][2]["timeout"], 0.25)

    def test_invalid_timeouts_are_rejected(self) -> None:
        for bad in (0.0, -1.0, math.nan, math.inf, 301.0):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                CatalogClient("127.0.0.1:1", timeout=bad)
        c, _ = make_client()
        with self.assertRaises(ValueError):
            c.get_product("x", timeout=-1)


class MetadataTest(unittest.TestCase):
    def test_every_call_sends_a_fresh_request_id(self) -> None:
        c, stub = make_client()
        c.get_product("a")
        c.get_product("b")
        ids = [dict(kw["metadata"])[REQUEST_ID_KEY] for _, _, kw in stub.calls]
        self.assertEqual(len(set(ids)), 2)

    def test_add_product_always_sends_an_idempotency_key(self) -> None:
        # Retrying AddProduct on UNAVAILABLE is only safe because of this.
        c, stub = make_client()
        c.add_product(NewProduct("Kettle"))
        c.add_product(NewProduct("Kettle"), idempotency_key="mine")
        first, second = (req.request_id for _, req, _ in stub.calls)
        self.assertTrue(first)
        self.assertEqual(second, "mine")


class StreamTest(unittest.TestCase):
    def test_stopping_a_search_early_cancels_the_call(self) -> None:
        c, stub = make_client()
        stub.search_stream = FakeStream(
            [
                product_pb2.SearchProductsResponse(
                    product=product_pb2.Product(id=str(i))
                )
                for i in range(5)
            ]
        )
        results = c.search()
        self.assertEqual(next(results).id, "0")
        results.close()  # what breaking out of a for loop does
        self.assertTrue(stub.search_stream.cancelled)

    def test_quote_maps_statuses_and_totals(self) -> None:
        c, stub = make_client()
        stub.quote_stream = FakeStream(
            [
                product_pb2.QuoteProductsResponse(
                    product_id="a",
                    quantity=2,
                    status=product_pb2.QUOTE_STATUS_OK,
                    total_cents=10,
                ),
                product_pb2.QuoteProductsResponse(
                    product_id="b",
                    quantity=1,
                    status=product_pb2.QUOTE_STATUS_NOT_FOUND,
                ),
            ]
        )
        quotes = list(c.quote([("a", 2), ("b", 1)]))
        self.assertEqual(quotes[0].status, QuoteStatus.OK)
        self.assertEqual(quotes[0].total_cents, 10)
        self.assertEqual(quotes[1].status, QuoteStatus.NOT_FOUND)
        self.assertIsNone(quotes[1].total_cents)

    def test_unknown_quote_status_is_an_error_not_a_crash(self) -> None:
        c, stub = make_client()
        stub.quote_stream = FakeStream(
            [
                product_pb2.QuoteProductsResponse(
                    product_id="a", status=product_pb2.QuoteStatus.ValueType(99)
                )
            ]
        )
        with self.assertRaises(CatalogError) as ctx:
            list(c.quote([("a", 1)]))
        self.assertEqual(ctx.exception.code, grpc.StatusCode.UNKNOWN)

    def test_error_mid_stream_becomes_catalog_error(self) -> None:
        c, stub = make_client()
        stub.search_stream = FakeStream(
            [], FakeRpcError(grpc.StatusCode.UNAVAILABLE, "gone")
        )
        with self.assertRaises(UnavailableError):
            list(c.search())


class ErrorMappingTest(unittest.TestCase):
    def test_invalid_argument_carries_field_violations(self) -> None:
        bad = error_details_pb2.BadRequest(
            field_violations=[
                error_details_pb2.BadRequest.FieldViolation(
                    field="product.name", description="must not be empty"
                )
            ]
        )
        err = from_rpc_error(
            FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "bad", [bad]), "r1"
        )
        self.assertIsInstance(err, InvalidArgumentError)
        self.assertIsInstance(err, ValueError)
        assert isinstance(err, InvalidArgumentError)  # narrows for mypy
        self.assertEqual(err.violations[0].field, "product.name")
        self.assertEqual(err.request_id, "r1")

    def test_not_found_carries_resource_name(self) -> None:
        info = error_details_pb2.ResourceInfo(
            resource_type="ecommerce.v1.Product", resource_name="p-9"
        )
        err = from_rpc_error(
            FakeRpcError(grpc.StatusCode.NOT_FOUND, "nope", [info]), "r"
        )
        assert isinstance(err, NotFoundError)
        self.assertIsInstance(err, LookupError)
        self.assertEqual(err.resource_name, "p-9")

    def test_codes_map_to_builtin_compatible_exceptions(self) -> None:
        cases = {
            grpc.StatusCode.DEADLINE_EXCEEDED: TimeoutError,
            grpc.StatusCode.UNAVAILABLE: ConnectionError,
            grpc.StatusCode.INTERNAL: CatalogError,
        }
        for code, builtin in cases.items():
            with self.subTest(code=code):
                self.assertIsInstance(
                    from_rpc_error(FakeRpcError(code), "r"), builtin
                )

    def test_cancelled_after_deadline_is_reported_as_deadline_exceeded(
        self,
    ) -> None:
        # gRPC-Go resets a stream with CANCEL when its copy of the deadline
        # fires, which can beat the client's own deadline timer.
        late = FakeRpcError(grpc.StatusCode.CANCELLED, time_remaining=0.0)
        self.assertIsInstance(from_rpc_error(late, "r"), DeadlineExceededError)
        early = FakeRpcError(grpc.StatusCode.CANCELLED, time_remaining=3.0)
        self.assertIsInstance(from_rpc_error(early, "r"), CancelledError)
        no_deadline = FakeRpcError(
            grpc.StatusCode.CANCELLED, time_remaining=None
        )
        self.assertIsInstance(from_rpc_error(no_deadline, "r"), CancelledError)


class ConfigTest(unittest.TestCase):
    def test_retry_policy_only_covers_retry_safe_methods(self) -> None:
        config = json.loads(client.SERVICE_CONFIG)
        (method_config,) = config["methodConfig"]
        methods = {n["method"] for n in method_config["name"]}
        self.assertEqual(
            methods, {"AddProduct", "GetProduct", "SearchProducts"}
        )
        self.assertEqual(
            method_config["retryPolicy"]["retryableStatusCodes"],
            ["UNAVAILABLE"],
        )

    def test_keepalive_respects_the_servers_enforcement_policy(self) -> None:
        # The server disconnects clients pinging more often than every 10 s.
        options = dict(client.channel_options())
        self.assertGreaterEqual(int(options["grpc.keepalive_time_ms"]), 10_000)


class SourceTest(unittest.TestCase):
    def test_assert_for_control_flow_bug_package_has_no_asserts(self) -> None:
        # The first client checked results with `assert`, which `python -O`
        # strips, silently turning failures into successes.
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            asserts = [
                n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)
            ]
            with self.subTest(file=path.name):
                self.assertEqual(asserts, [])


if __name__ == "__main__":
    unittest.main()
