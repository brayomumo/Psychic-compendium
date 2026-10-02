"""Typed client for ecommerce.v1.ProductCatalogService.

Every call carries a deadline (an RPC without one can wait forever), a fresh
``x-request-id`` for correlating client and server logs, and turns gRPC
errors into exceptions that carry the server's structured error details.
"""

import enum
import json
import logging
import math
import uuid
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Self, cast

import grpc
from google.rpc import error_details_pb2
from grpc_status import rpc_status

from ecommerce.v1 import product_pb2, product_pb2_grpc

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_TIMEOUT_S",
    "REQUEST_ID_KEY",
    "SERVICE_NAME",
    "AlreadyExistsError",
    "CancelledError",
    "CatalogClient",
    "CatalogError",
    "DeadlineExceededError",
    "FailedPreconditionError",
    "FieldViolation",
    "InvalidArgumentError",
    "NewProduct",
    "NotFoundError",
    "Product",
    "Quote",
    "QuoteStatus",
    "ResourceExhaustedError",
    "UnavailableError",
    "channel_options",
    "from_rpc_error",
]

logger = logging.getLogger(__name__)

SERVICE_NAME: Final = "ecommerce.v1.ProductCatalogService"
REQUEST_ID_KEY: Final = "x-request-id"
DEFAULT_TIMEOUT_S: Final = 5.0
MAX_TIMEOUT_S: Final = 300.0

# Retry UNAVAILABLE (server restarting, connection dropped) with backoff,
# but only for calls that are safe to repeat. AddProduct qualifies because
# this client always sends an idempotency key with it. The streaming calls
# that send data are left out: a retried stream would replay its items.
_RETRY_POLICY: Final = {
    "maxAttempts": 4,
    "initialBackoff": "0.1s",
    "maxBackoff": "1s",
    "backoffMultiplier": 2,
    "retryableStatusCodes": ["UNAVAILABLE"],
}
RETRYABLE_METHODS: Final = ("AddProduct", "GetProduct", "SearchProducts")
SERVICE_CONFIG: Final = json.dumps(
    {
        "methodConfig": [
            {
                "name": [
                    {"service": SERVICE_NAME, "method": m}
                    for m in RETRYABLE_METHODS
                ],
                "retryPolicy": _RETRY_POLICY,
            }
        ]
    }
)


def channel_options() -> list[tuple[str, int | str]]:
    """Returns the channel options this client uses.

    Keepalive pings detect a dead connection, but the server disconnects a
    client that pings more often than every 10 s (its enforcement policy),
    so the client pings every 30 s.

    Returns:
        gRPC channel options.
    """
    return [
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.enable_retries", 1),
        ("grpc.service_config", SERVICE_CONFIG),
    ]


@dataclass(frozen=True)
class Product:
    """A stored product."""

    id: str
    name: str
    description: str
    price_cents: int


@dataclass(frozen=True)
class NewProduct:
    """A product to create; the server assigns its ID."""

    name: str
    description: str = ""
    price_cents: int = 0


class QuoteStatus(enum.Enum):
    """Outcome of one quote."""

    OK = product_pb2.QUOTE_STATUS_OK
    NOT_FOUND = product_pb2.QUOTE_STATUS_NOT_FOUND
    INVALID_QUANTITY = product_pb2.QUOTE_STATUS_INVALID_QUANTITY


@dataclass(frozen=True)
class Quote:
    """The price of a quantity of one product, or why there is none."""

    product_id: str
    quantity: int
    status: QuoteStatus
    total_cents: int | None


@dataclass(frozen=True)
class FieldViolation:
    """One invalid request field, from a google.rpc.BadRequest detail."""

    field: str
    description: str


class CatalogError(Exception):
    """A failed RPC.

    Attributes:
        code: The gRPC status code.
        message: The server's message.
        request_id: The x-request-id sent with the call, for finding it in
            the server's logs.
    """

    def __init__(
        self, code: grpc.StatusCode, message: str, request_id: str
    ) -> None:
        """Initializes the error.

        Args:
            code: The gRPC status code.
            message: The server's message.
            request_id: The x-request-id sent with the call.
        """
        super().__init__(f"{code.name}: {message} (request {request_id})")
        self.code = code
        self.message = message
        self.request_id = request_id


class InvalidArgumentError(CatalogError, ValueError):
    """The request was invalid; ``violations`` says which fields and why."""

    violations: tuple[FieldViolation, ...] = ()


class NotFoundError(CatalogError, LookupError):
    """The named product does not exist."""

    resource_name: str = ""


class AlreadyExistsError(CatalogError):
    """A product with that name already exists."""

    resource_name: str = ""


class FailedPreconditionError(CatalogError):
    """The request conflicts with state, e.g. a reused idempotency key."""


class ResourceExhaustedError(CatalogError):
    """A server limit was hit: catalog capacity or message size."""


class DeadlineExceededError(CatalogError, TimeoutError):
    """The call did not finish before its deadline."""


class UnavailableError(CatalogError, ConnectionError):
    """The server could not be reached, even after retries."""


class CancelledError(CatalogError):
    """The call was cancelled."""


_ERRORS: Final[dict[grpc.StatusCode, type[CatalogError]]] = {
    grpc.StatusCode.INVALID_ARGUMENT: InvalidArgumentError,
    grpc.StatusCode.NOT_FOUND: NotFoundError,
    grpc.StatusCode.ALREADY_EXISTS: AlreadyExistsError,
    grpc.StatusCode.FAILED_PRECONDITION: FailedPreconditionError,
    grpc.StatusCode.RESOURCE_EXHAUSTED: ResourceExhaustedError,
    grpc.StatusCode.DEADLINE_EXCEEDED: DeadlineExceededError,
    grpc.StatusCode.UNAVAILABLE: UnavailableError,
    grpc.StatusCode.CANCELLED: CancelledError,
}


def from_rpc_error(err: grpc.RpcError, request_id: str) -> CatalogError:
    """Converts a gRPC error into the matching CatalogError.

    Args:
        err: The error a stub raised. Errors raised by grpc stubs are also
            ``grpc.Call`` objects, which carry the code and trailers.
        request_id: The x-request-id sent with the call.

    Returns:
        The CatalogError subclass for the status code, with any structured
        details (field violations, resource name) filled in.
    """
    if not callable(getattr(err, "code", None)):
        return CatalogError(grpc.StatusCode.UNKNOWN, str(err), request_id)
    # The typeshed stubs declare RpcError and Call as incompatible, but every
    # error a grpc stub raises implements both.
    call = cast("grpc.Call", err)
    code, message = call.code(), call.details() or ""
    if code is grpc.StatusCode.CANCELLED and _deadline_passed(call):
        # The server's copy of the deadline fired first: gRPC-Go then resets
        # the stream with CANCEL, which arrives before this client's own
        # timer (rounded up to whole milliseconds) fires. The call still died
        # of its deadline, so report it as one.
        code = grpc.StatusCode.DEADLINE_EXCEEDED
    error = _ERRORS.get(code, CatalogError)(code, message, request_id)
    status = rpc_status.from_call(call)
    if status is None:
        return error
    for detail in status.details:
        if detail.Is(error_details_pb2.BadRequest.DESCRIPTOR) and isinstance(
            error, InvalidArgumentError
        ):
            bad = error_details_pb2.BadRequest()
            detail.Unpack(bad)
            error.violations = tuple(
                FieldViolation(v.field, v.description)
                for v in bad.field_violations
            )
        elif detail.Is(error_details_pb2.ResourceInfo.DESCRIPTOR) and (
            isinstance(error, NotFoundError | AlreadyExistsError)
        ):
            info = error_details_pb2.ResourceInfo()
            detail.Unpack(info)
            error.resource_name = info.resource_name
    return error


def _deadline_passed(call: grpc.Call) -> bool:
    # The stubs say float, but grpc returns None for a call with no deadline.
    remaining: float | None = call.time_remaining()
    return remaining is not None and remaining <= 0


def _product(p: product_pb2.Product) -> Product:
    return Product(p.id, p.name, p.description, p.price_cents)


def _new_product(p: NewProduct) -> product_pb2.NewProduct:
    return product_pb2.NewProduct(
        name=p.name, description=p.description, price_cents=p.price_cents
    )


def _check_timeout(timeout: float) -> float:
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_S:
        raise ValueError(
            f"timeout must be in (0, {MAX_TIMEOUT_S}] seconds, got {timeout}"
        )
    return timeout


class CatalogClient:
    """A ProductCatalogService client. Use it as a context manager.

    Every method takes an optional ``timeout`` in seconds; without one the
    client's default applies. No call is ever made without a deadline.
    """

    def __init__(
        self,
        target: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        channel: grpc.Channel | None = None,
    ) -> None:
        """Opens a channel to ``target`` (connection happens lazily).

        Args:
            target: ``host:port`` of the server.
            timeout: Default per-call deadline, in seconds.
            channel: An existing channel to use instead of opening one.

        Raises:
            ValueError: ``timeout`` is not a positive, finite number of
                seconds up to MAX_TIMEOUT_S.
        """
        self._timeout = _check_timeout(timeout)
        self._channel = channel or grpc.insecure_channel(
            target, options=channel_options()
        )
        self._stub = product_pb2_grpc.ProductCatalogServiceStub(self._channel)

    def __enter__(self) -> Self:
        """Returns the client."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Closes the channel."""
        self.close()

    def close(self) -> None:
        """Closes the channel, cancelling any call still running on it."""
        self._channel.close()

    def _call_args(
        self, timeout: float | None
    ) -> tuple[float, tuple[tuple[str, str | bytes], ...], str]:
        request_id = str(uuid.uuid4())
        deadline = self._timeout if timeout is None else _check_timeout(timeout)
        return deadline, ((REQUEST_ID_KEY, request_id),), request_id

    def add_product(
        self,
        product: NewProduct,
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> Product:
        """Creates a product.

        Args:
            product: What to create.
            idempotency_key: Sent as request_id. A repeated key returns the
                product the first call created. A fresh UUID is used when
                omitted, which is what makes automatic retries safe.
            timeout: Deadline in seconds.

        Returns:
            The stored product, with its server-assigned ID.

        Raises:
            CatalogError: The server rejected the call (see subclasses).
        """
        deadline, metadata, request_id = self._call_args(timeout)
        request = product_pb2.AddProductRequest(
            product=_new_product(product),
            request_id=idempotency_key or str(uuid.uuid4()),
        )
        try:
            response = self._stub.AddProduct(
                request, timeout=deadline, metadata=metadata
            )
        except grpc.RpcError as err:
            raise from_rpc_error(err, request_id) from err
        return _product(response.product)

    def get_product(
        self, product_id: str, *, timeout: float | None = None
    ) -> Product:
        """Fetches one product.

        Args:
            product_id: The product's ID.
            timeout: Deadline in seconds.

        Returns:
            The product.

        Raises:
            CatalogError: The server rejected the call (see subclasses).
        """
        deadline, metadata, request_id = self._call_args(timeout)
        try:
            response = self._stub.GetProduct(
                product_pb2.GetProductRequest(id=product_id),
                timeout=deadline,
                metadata=metadata,
            )
        except grpc.RpcError as err:
            raise from_rpc_error(err, request_id) from err
        return _product(response.product)

    def search(
        self, query: str = "", *, timeout: float | None = None
    ) -> Generator[Product, None, None]:
        """Streams products matching ``query`` (server streaming).

        The deadline covers the whole stream. Stopping iteration early
        cancels the call, so the server stops sending.

        Args:
            query: Case-insensitive substring of name or description.
            timeout: Deadline in seconds for the whole stream.

        Yields:
            Matching products, ordered by name.

        Raises:
            CatalogError: The server rejected or ended the call.
        """
        deadline, metadata, request_id = self._call_args(timeout)
        call = self._stub.SearchProducts(
            product_pb2.SearchProductsRequest(query=query),
            timeout=deadline,
            metadata=metadata,
        )
        try:
            for response in call:
                yield _product(response.product)
        except grpc.RpcError as err:
            raise from_rpc_error(err, request_id) from err
        finally:
            call.cancel()  # a no-op once the stream has ended

    def bulk_add(
        self, products: Iterable[NewProduct], *, timeout: float | None = None
    ) -> list[Product]:
        """Adds products atomically: all of them or none (client streaming).

        Args:
            products: What to create, streamed to the server lazily.
            timeout: Deadline in seconds for the whole call.

        Returns:
            The stored products, in the order given.

        Raises:
            CatalogError: The server rejected the batch; nothing was stored.
        """
        deadline, metadata, request_id = self._call_args(timeout)
        requests = (
            product_pb2.BulkAddProductsRequest(product=_new_product(p))
            for p in products
        )
        try:
            response = self._stub.BulkAddProducts(
                requests, timeout=deadline, metadata=metadata
            )
        except grpc.RpcError as err:
            raise from_rpc_error(err, request_id) from err
        return [_product(p) for p in response.products]

    def quote(
        self,
        items: Iterable[tuple[str, int]],
        *,
        timeout: float | None = None,
    ) -> Generator[Quote, None, None]:
        """Prices (product ID, quantity) pairs as they stream (bidirectional).

        Args:
            items: Pairs to price, sent lazily as the server answers.
            timeout: Deadline in seconds for the whole stream.

        Yields:
            One Quote per item, in order. A bad item yields a Quote with an
            error status instead of ending the stream.

        Raises:
            CatalogError: The call itself failed.
        """
        deadline, metadata, request_id = self._call_args(timeout)
        requests = (
            product_pb2.QuoteProductsRequest(product_id=pid, quantity=qty)
            for pid, qty in items
        )
        call = self._stub.QuoteProducts(
            requests, timeout=deadline, metadata=metadata
        )
        try:
            for r in call:
                try:
                    status = QuoteStatus(r.status)
                except ValueError:
                    # A newer server may send a status this client predates.
                    raise CatalogError(
                        grpc.StatusCode.UNKNOWN,
                        f"unknown quote status {r.status}",
                        request_id,
                    ) from None
                total = r.total_cents if status is QuoteStatus.OK else None
                yield Quote(r.product_id, r.quantity, status, total)
        except grpc.RpcError as err:
            raise from_rpc_error(err, request_id) from err
        finally:
            call.cancel()
