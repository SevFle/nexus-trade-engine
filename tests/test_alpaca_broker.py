"""Unit tests for :class:`engine.brokers.alpaca.AlpacaBrokerAdapter`.

These tests never touch the network: the Alpaca REST API is simulated with a
``httpx.MockTransport``-backed :class:`httpx.AsyncClient` injected into the
adapter (the same pattern :mod:`tests.test_live_backend` and the data-provider
tests use). Because the adapter delegates every HTTP call to the existing
:class:`~engine.execution.live_backend.LiveExecutionBackend`, a single mock
client exercises the full request → response → typed-error path.

Coverage (per the adapter spec):
- successful **market** order submission
- successful **limit** order submission (limit_price in the body)
- **auth failure** (HTTP 401 → ``BrokerAuthError``)
- **invalid symbol** order submission (HTTP 422 → ``BrokerRejectError``)
- **position query** happy path (parses into ``BrokerPosition``)
- position query for a symbol with no position (HTTP 404 → ``BrokerRejectError``)
- ``cancel_order`` happy path (DELETE /v2/orders/{id})
- adapter-level guard: a limit order without ``limit_price`` raises ``ValueError``
- construction: paper/live base URL selection + credential requirement
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from engine.brokers import AlpacaBrokerAdapter
from engine.brokers.alpaca import AlpacaBrokerAdapter as AlpacaBrokerAdapterFromModule
from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
)
from engine.core.brokers.models import BrokerPosition

# ---------------------------------------------------------------------------
# Mock-transport helpers
# ---------------------------------------------------------------------------

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


def _mock_client(
    handler: Any, base_url: str = PAPER_URL
) -> httpx.AsyncClient:
    """Build an AsyncClient backed by ``handler`` (no real network)."""
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


def _authed_handler(routes: dict[str, Any]) -> Any:
    """Route a request to a canned response keyed by ``"METHOD path"``.

    A route value of ``("status", json_body)`` builds a JSON response. An
    ``httpx.Response`` is returned as-is. Any unrecognised request returns a
    404 so a misrouted call fails loudly instead of silently passing.
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


def _new_submitted_order(
    *,
    symbol: str = "AAPL",
    side: str = "buy",
    order_type: str = "market",
    qty: str = "100",
    order_id: str = "61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01",
) -> dict[str, Any]:
    """Alpaca-shaped order response for a freshly accepted order."""
    return {
        "id": order_id,
        "client_order_id": "client-abc",
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "qty": qty,
        "status": "new",
        "filled_qty": "0",
        "filled_avg_price": None,
        "created_at": "2026-07-08T10:00:00Z",
    }


def _new_position(symbol: str = "AAPL") -> dict[str, Any]:
    """Alpaca-shaped single-position response."""
    return {
        "symbol": symbol,
        "qty": "100",
        "side": "long",
        "avg_entry_price": "150.00",
        "market_value": "15500.00",
        "unrealized_pl": "500.00",
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_package_reexport_matches_module_class():
    assert AlpacaBrokerAdapter is AlpacaBrokerAdapterFromModule


def test_name_is_alpaca():
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s")
    assert adapter.name == "alpaca"


def test_init_defaults_to_paper_endpoint():
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s")
    assert adapter.backend.paper is True
    assert adapter.backend.base_url == PAPER_URL


def test_init_live_endpoint_when_paper_false():
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", paper=False)
    assert adapter.backend.base_url == LIVE_URL


def test_init_requires_credentials():
    for key, secret in [("", "secret"), ("key", ""), ("", "")]:
        with pytest.raises(ValueError, match="api_key and api_secret"):
            AlpacaBrokerAdapter(api_key=key, api_secret=secret)


# ---------------------------------------------------------------------------
# 1) submit_order — successful market order
# ---------------------------------------------------------------------------


async def test_submit_order_market_posts_order_and_returns_broker_payload():
    """A market order POSTs to /v2/orders with the right body + auth headers
    and returns the broker's order JSON unchanged."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_new_submitted_order())

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="PKTESTKEY", api_secret="SECRETTEST", client=client)

    order = await adapter.submit_order(
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

    # The body mirrors the Alpaca order shape we expect to send. A market
    # order must NOT carry a limit_price.
    body = req.read().decode()
    assert '"symbol":"AAPL"' in body
    assert '"side":"buy"' in body
    assert '"type":"market"' in body
    assert '"qty":"100"' in body
    assert '"time_in_force":"day"' in body
    assert '"client_order_id":"client-abc"' in body
    assert "limit_price" not in body

    await client.aclose()


# ---------------------------------------------------------------------------
# 2) submit_order — successful limit order
# ---------------------------------------------------------------------------


async def test_submit_order_limit_includes_limit_price_in_body():
    """A limit order must serialise limit_price into the POST body."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_new_submitted_order(order_type="limit"))

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    order = await adapter.submit_order(
        "AAPL", Decimal("50"), "sell", "LIMIT", limit_price=Decimal("330.50")
    )

    assert order["id"] == "61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01"
    assert len(captured) == 1

    body = captured[0].read().decode()
    # order_type normalised to lower case; limit_price serialised as a string.
    assert '"type":"limit"' in body
    assert '"side":"sell"' in body
    assert '"qty":"50"' in body
    assert '"limit_price":"330.5"' in body
    await client.aclose()


async def test_submit_order_limit_without_price_raises_value_error():
    """A limit order missing its limit_price is rejected before any HTTP call.

    The adapter validates this up front so the caller gets a clear error
    instead of an opaque 422 from Alpaca after a round-trip.
    """
    client = _mock_client(lambda r: httpx.Response(200))
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    with pytest.raises(ValueError, match="limit_price"):
        await adapter.submit_order("AAPL", 10, "buy", "limit")

    await client.aclose()


# ---------------------------------------------------------------------------
# 3) submit_order — auth failure
# ---------------------------------------------------------------------------


async def test_submit_order_auth_failure_raises_broker_auth_error():
    """An HTTP 401 surfaces as BrokerAuthError (permanent; do not retry)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid API key"})

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="bad", api_secret="bad", client=client)

    with pytest.raises(BrokerAuthError):
        await adapter.submit_order("AAPL", 100, "buy", "market")

    await client.aclose()


# ---------------------------------------------------------------------------
# 4) submit_order — invalid symbol (broker rejection)
# ---------------------------------------------------------------------------


async def test_submit_order_invalid_symbol_raises_broker_reject_error():
    """Submitting an unknown symbol → HTTP 422 → BrokerRejectError carrying
    the broker's numeric code so the OMS can log the exact reason."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"code": 42210000, "message": "symbol is not found"},
        )

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    with pytest.raises(BrokerRejectError) as exc_info:
        await adapter.submit_order("NOPE", 100, "buy", "market")
    assert exc_info.value.broker_code == "42210000"
    assert "symbol is not found" in str(exc_info.value)

    await client.aclose()


# ---------------------------------------------------------------------------
# 5) get_position — happy path
# ---------------------------------------------------------------------------


async def test_get_position_returns_parsed_broker_position():
    """get_position GETs /v2/positions/{symbol} with auth headers and parses
    the response into a BrokerPosition."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_new_position("AAPL"))

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="PKTESTKEY", api_secret="SECRETTEST", client=client)

    position = await adapter.get_position("AAPL")

    assert isinstance(position, BrokerPosition)
    assert position.symbol == "AAPL"
    assert position.side == "long"
    assert position.is_long is True
    assert position.qty == Decimal("100")
    assert position.avg_entry_price == Decimal("150.00")
    assert position.market_value == Decimal("15500.00")
    assert position.unrealized_pl == Decimal("500.00")

    # Exactly one GET, to the right path, with auth headers.
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/v2/positions/AAPL"
    assert req.headers["APCA-API-KEY-ID"] == "PKTESTKEY"
    assert req.headers["APCA-API-SECRET-KEY"] == "SECRETTEST"

    await client.aclose()


async def test_get_position_no_position_raises_broker_reject_error():
    """A symbol with no open position → HTTP 404 → BrokerRejectError.

    The broker_code carries Alpaca's numeric code so callers can tell "no
    position" apart from a transport/auth failure."""
    handler = _authed_handler(
        {"GET /v2/positions/CASH": (404, {"code": 40410000, "message": "position does not exist"})}
    )
    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    with pytest.raises(BrokerRejectError) as exc_info:
        await adapter.get_position("CASH")
    assert exc_info.value.broker_code == "40410000"

    await client.aclose()


async def test_get_position_auth_failure_raises_broker_auth_error():
    """get_position shares the backend's auth mapping: a 401 → BrokerAuthError."""
    handler = _authed_handler({"GET /v2/positions/AAPL": (403, {"message": "forbidden"})})
    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="bad", api_secret="bad", client=client)

    with pytest.raises(BrokerAuthError):
        await adapter.get_position("AAPL")

    await client.aclose()


# ---------------------------------------------------------------------------
# 6) cancel_order — happy path
# ---------------------------------------------------------------------------


async def test_cancel_order_deletes_and_succeeds_on_204():
    """cancel_order issues DELETE /v2/orders/{id}; a 204 means success."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)  # Alpaca's success response for cancel

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    await adapter.cancel_order("61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/v2/orders/61e0a1d7-0b19-4f8e-8c2b-7f5b1b1f9a01"
    assert req.headers["APCA-API-KEY-ID"] == "k"
    await client.aclose()


async def test_cancel_order_unknown_order_raises_broker_reject_error():
    """Cancelling an unknown / already-terminal order (404) is a rejection."""
    handler = _authed_handler(
        {"DELETE /v2/orders/does-not-exist": (404, {"code": 40410000, "message": "order not found"})}
    )
    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)

    with pytest.raises(BrokerRejectError) as exc_info:
        await adapter.cancel_order("does-not-exist")
    assert exc_info.value.broker_code == "40410000"
    await client.aclose()


# ---------------------------------------------------------------------------
# connect — delegates to the backend's /v2/account probe
# ---------------------------------------------------------------------------


async def test_connect_probes_account_and_marks_backend_connected():
    client = _mock_client(_authed_handler({"GET /v2/account": (200, {"status": "ACTIVE"})}))
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)
    assert adapter.backend._connected is False
    await adapter.connect()
    assert adapter.backend._connected is True
    await client.aclose()


async def test_close_is_alias_for_disconnect():
    client = _mock_client(_authed_handler({"GET /v2/account": (200, {"status": "ACTIVE"})}))
    adapter = AlpacaBrokerAdapter(api_key="k", api_secret="s", client=client)
    await adapter.connect()
    await adapter.close()
    assert adapter.backend._connected is False
    await client.aclose()


# ---------------------------------------------------------------------------
# Error vocabulary is shared with the backend (transient → connection error)
# ---------------------------------------------------------------------------


async def test_transient_503_exhausts_retries_then_raises_connection_error():
    """A persistent 503 is retried then surfaced as BrokerConnectionError,
    proving the adapter inherits the backend's retry policy for free."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"message": "service unavailable"})

    client = _mock_client(handler)
    adapter = AlpacaBrokerAdapter(
        api_key="k", api_secret="s", client=client, max_retries=2, retry_backoff_s=0
    )
    with pytest.raises(BrokerConnectionError):
        await adapter.get_position("AAPL")
    # First attempt + one retry == 2 total.
    assert calls["n"] == 2
    await client.aclose()
