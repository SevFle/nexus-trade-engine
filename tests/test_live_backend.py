"""Unit tests for :class:`engine.execution.live_backend.LiveExecutionBackend`.

These tests never touch the network: the broker REST API is simulated with a
``httpx.MockTransport``-backed :class:`httpx.AsyncClient` (the same pattern
:mod:`engine.core.brokers.alpaca` and the data-provider tests use).

Coverage:
- ``test_submit_order_posts_order_and_returns_broker_payload`` — happy-path
  order submission verifies the request method/path/body + auth headers and
  that the broker's JSON is returned.
- ``test_cancel_order_deletes_and_succeeds_on_204`` — happy-path cancellation.
- ``test_get_order_status_polls_and_returns_payload`` — happy-path status poll.

Plus error-mapping + ABC-integration tests that lock in the typed error
contract (``BrokerAuthError`` / ``BrokerRejectError`` / ``BrokerConnectionError``)
and the ``connect`` / ``execute`` → :class:`FillResult` path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
)
from engine.core.execution.base import ExecutionBackend
from engine.execution.live_backend import (
    LIVE_BASE_URL,
    PAPER_BASE_URL,
    LiveExecutionBackend,
)

# ---------------------------------------------------------------------------
# Mock-transport client factory
# ---------------------------------------------------------------------------


def _mock_client(
    handler: Any, base_url: str = "https://paper-api.alpaca.markets"
) -> httpx.AsyncClient:
    """Build an AsyncClient backed by ``handler`` (no real network)."""
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


def _authed_handler(
    routes: dict[str, Any],
) -> Any:
    """Route a request to a canned response keyed by ``"METHOD path"``.

    Routes can also be ``("status_code", json_body)`` tuples for brevity.
    Any unrecognised request returns a 404 so a misrouted call fails loudly
    instead of silently passing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        spec = routes.get(key)
        if spec is None:
            return httpx.Response(404, json={"message": f"no route for {key}"})
        if isinstance(spec, httpx.Response):
            return spec
        status, body = spec
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=body)

    return handler


def _new_submitted_order() -> dict[str, Any]:
    """Alpaca-shaped order response for a freshly accepted order."""
    return {
        "id": "61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01",
        "client_order_id": "client-abc",
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "100",
        "status": "new",
        "filled_qty": "0",
        "filled_avg_price": None,
        "created_at": "2026-07-02T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# Construction / ABC conformance
# ---------------------------------------------------------------------------


def test_is_execution_backend_subclass():
    assert issubclass(LiveExecutionBackend, ExecutionBackend)


def test_init_defaults_to_paper_endpoint():
    backend = LiveExecutionBackend(api_key="key", api_secret="secret")
    assert backend.paper is True
    assert backend.base_url == PAPER_BASE_URL
    assert backend._connected is False


def test_init_live_endpoint_when_paper_false():
    backend = LiveExecutionBackend(api_key="key", api_secret="secret", paper=False)
    assert backend.base_url == LIVE_BASE_URL


def test_init_requires_credentials():
    for key, secret in [("", "secret"), ("key", ""), ("", "")]:
        with pytest.raises(ValueError, match="api_key and api_secret"):
            LiveExecutionBackend(api_key=key, api_secret=secret)


# ---------------------------------------------------------------------------
# 1) submit_order — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_order_posts_order_and_returns_broker_payload():
    """submit_order POSTs to /v2/orders with the right body + auth headers
    and returns the broker's order JSON unchanged."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_new_submitted_order())

    client = _mock_client(handler)
    backend = LiveExecutionBackend(api_key="PKTESTKEY", api_secret="SECRETTEST", client=client)

    order = await backend.submit_order(
        "AAPL", Decimal("100"), "buy", "market", client_order_id="client-abc"
    )

    # Returns the broker payload verbatim.
    assert order["id"] == "61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01"
    assert order["status"] == "new"

    # Exactly one request, to the right method/path.
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v2/orders"

    # Auth headers travelled with the request.
    assert req.headers["APCA-API-KEY-ID"] == "PKTESTKEY"
    assert req.headers["APCA-API-SECRET-KEY"] == "SECRETTEST"

    # The body mirrors the Alpaca order shape we expect to send.
    body = req.read().decode()
    assert '"symbol":"AAPL"' in body
    assert '"side":"buy"' in body
    assert '"type":"market"' in body
    assert '"qty":"100"' in body
    assert '"time_in_force":"day"' in body
    assert '"client_order_id":"client-abc"' in body

    await client.aclose()


@pytest.mark.asyncio
async def test_submit_order_normalises_side_and_symbol():
    """Side enums / upper-case symbols are normalised to Alpaca's shape."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_new_submitted_order())

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)

    class _Side:
        value = "BUY"

    await backend.submit_order("msft", 5, _Side(), "LIMIT", limit_price=Decimal("330.5"))

    body = captured[0].read().decode()
    assert '"symbol":"MSFT"' in body  # upper-cased
    assert '"side":"buy"' in body  # enum + upper-cased normalised
    assert '"type":"limit"' in body
    assert '"qty":"5"' in body
    assert '"limit_price":"330.5"' in body
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_order_rejection_raises_broker_reject_error():
    """A 422 (insufficient buying power) maps to BrokerRejectError with code."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"code": 4221000, "message": "insufficient buying power"},
        )

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)
    with pytest.raises(BrokerRejectError) as exc_info:
        await backend.submit_order("AAPL", 100, "buy", "market")
    assert exc_info.value.broker_code == "4221000"
    assert "insufficient buying power" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_order_auth_failure_raises_broker_auth_error():

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid key"})

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)
    with pytest.raises(BrokerAuthError):
        await backend.submit_order("AAPL", 100, "buy", "market")
    await client.aclose()


# ---------------------------------------------------------------------------
# 2) cancel_order — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_deletes_and_succeeds_on_204():
    """cancel_order issues DELETE /v2/orders/{id}; a 204 means success."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)  # Alpaca's success response for cancel

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)

    # No exception on the happy path.
    await backend.cancel_order("61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/v2/orders/61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01"
    assert req.headers["APCA-API-KEY-ID"] == "k"
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_order_unknown_order_raises_broker_reject_error():
    """Cancelling an unknown / already-terminal order (404) is a rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": 40410000, "message": "order not found"})

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)
    with pytest.raises(BrokerRejectError) as exc_info:
        await backend.cancel_order("does-not-exist")
    assert exc_info.value.broker_code == "40410000"
    await client.aclose()


# ---------------------------------------------------------------------------
# 3) get_order_status — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_order_status_polls_and_returns_payload():
    """get_order_status GETs /v2/orders/{id} and returns the parsed JSON."""
    captured: list[httpx.Request] = []
    filled = {
        "id": "ord-123",
        "symbol": "AAPL",
        "side": "buy",
        "status": "filled",
        "qty": "100",
        "filled_qty": "100",
        "filled_avg_price": "150.25",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=filled)

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)

    status = await backend.get_order_status("ord-123")

    assert status["status"] == "filled"
    assert status["filled_qty"] == "100"
    assert status["filled_avg_price"] == "150.25"

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/v2/orders/ord-123"
    await client.aclose()


# ---------------------------------------------------------------------------
# connect / disconnect (ABC) — mocked /v2/account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_hits_account_endpoint_and_marks_connected():
    client = _mock_client(_authed_handler({"GET /v2/account": (200, {"status": "ACTIVE"})}))
    backend = LiveExecutionBackend("k", "s", client=client)
    assert backend._connected is False
    await backend.connect()
    assert backend._connected is True
    assert backend._connected_at is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_rejects_bad_credentials():
    client = _mock_client(_authed_handler({"GET /v2/account": (403, {"message": "forbidden"})}))
    backend = LiveExecutionBackend("k", "s", client=client)
    with pytest.raises(BrokerAuthError):
        await backend.connect()
    assert backend._connected is False
    await client.aclose()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent_and_closes_owned_client():
    client = _mock_client(_authed_handler({"GET /v2/account": (200, {"status": "ACTIVE"})}))
    backend = LiveExecutionBackend("k", "s", client=client)
    await backend.connect()
    await backend.disconnect()
    assert backend._connected is False
    assert backend._connected_at is None
    # Calling again must not raise even though state is already cleared.
    await backend.disconnect()


# ---------------------------------------------------------------------------
# execute() — ABC conformance, routes to submit_order → FillResult
# ---------------------------------------------------------------------------


class _FakeSide:
    value = "buy"


class _FakeOrderType:
    value = "market"


class _FakeOrder:
    def __init__(self) -> None:
        self.id = "ord-internal-1"
        self.symbol = "AAPL"
        self.quantity = Decimal("100")
        self.side = _FakeSide()
        self.order_type = _FakeOrderType()
        self.limit_price = None


@pytest.mark.asyncio
async def test_execute_routes_to_submit_order_returns_success():
    """A connected backend.translate an Order into submit_order and returns
    a FillResult; an acknowledged-but-unfilled order still reports success
    with zero fill quantity."""
    accepted = _new_submitted_order()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=accepted)

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)
    await backend.connect()

    result = await backend.execute(_FakeOrder(), 150.0, costs=None)
    assert result.success is True
    # Accepted, not yet filled → no fill recorded yet, broker id in reason.
    assert result.quantity == 0
    assert result.price == 0.0
    assert result.reason == accepted["id"]
    await client.aclose()


@pytest.mark.asyncio
async def test_execute_not_connected_returns_structured_failure():
    backend = LiveExecutionBackend("k", "s", client=_mock_client(lambda r: httpx.Response(200)))
    # Deliberately NOT connected.
    result = await backend.execute(_FakeOrder(), 150.0, costs=None)
    assert result.success is False
    assert "not connected" in result.reason.lower()


@pytest.mark.asyncio
async def test_execute_maps_broker_rejection_to_fill_result():
    def handler(request: httpx.Request) -> httpx.Response:
        # connect() probes /v2/account; let it succeed, reject the order POST.
        if request.url.path == "/v2/account":
            return httpx.Response(200, json={"status": "ACTIVE"})
        return httpx.Response(422, json={"code": 4221000, "message": "insufficient buying power"})

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client)
    await backend.connect()

    result = await backend.execute(_FakeOrder(), 150.0, costs=None)
    assert result.success is False
    assert "insufficient buying power" in result.reason
    await client.aclose()


# ---------------------------------------------------------------------------
# Error mapping — transient → BrokerConnectionError (after retries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_503_exhausts_retries_then_raises_connection_error():
    """A persistent 503 is retried then surfaced as BrokerConnectionError."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"message": "service unavailable"})

    client = _mock_client(handler)
    backend = LiveExecutionBackend("k", "s", client=client, max_retries=2, retry_backoff_s=0)
    with pytest.raises(BrokerConnectionError):
        await backend.get_order_status("ord-1")
    # First attempt + one retry == 2 total.
    assert calls["n"] == 2
    await client.aclose()
