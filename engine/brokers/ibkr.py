"""Interactive Brokers (IBKR) broker adapter (SEV-223+).

A broker-direct adapter (:class:`IBKRBrokerAdapter`) that implements the
ExecutionBackend-facing surface the engine expects from an IBKR
integration, talking to IBKR's Client Portal Web API / Cloud API over an
:class:`httpx.AsyncClient`. It exposes:

- :meth:`IBKRBrokerAdapter.connect` / :meth:`disconnect` /
  :meth:`is_started` — session lifecycle (``GET /v1/api/auth/status``)
- :meth:`IBKRBrokerAdapter.place_order`  — ``POST /v1/api/iserver/account/{acct}/orders``
  (with engine → IBKR field mapping: symbol → ``conid``, market/limit → MKT/LMT,
  buy/sell → BUY/SELL)
- :meth:`IBKRBrokerAdapter.cancel_order` — ``DELETE /v1/api/iserver/account/{acct}/order/{id}``
- :meth:`IBKRBrokerAdapter.get_positions` — ``GET /v1/api/portfolio/{acct}/positions``
- :meth:`IBKRBrokerAdapter.get_account`   — ``GET /v1/api/portfolio/{acct}/summary``

Why a dedicated request pipeline (rather than reusing
:class:`~engine.execution.live_backend.LiveExecutionBackend`)
----------------------------------------------------------------------
IBKR's REST surface is structurally different from Alpaca's — every
order / portfolio call is scoped under an account id, orders are submitted
as a ``{"orders": [...]}`` array keyed by integer ``conid`` (not a symbol),
the enum vocabulary is upper-case (``MKT``/``LMT``, ``BUY``/``SELL``), and
auth is session/bearer-token based. Reusing the Alpaca-hardwired backend
would mean fighting its ``/v2/...`` paths and ``APCA-*`` headers on every
call. Instead this adapter owns its own thin transport pipeline (modelled
on :class:`~engine.core.brokers.alpaca.AlpacaTradingClient`) while reusing
the *exact same* typed error vocabulary from :mod:`engine.core.brokers.base`,
so the live loop and OMS react to failures identically across brokers:

- HTTP **401 / 403**        → :class:`~engine.core.brokers.base.BrokerAuthError`
  (permanent; engage kill-switch)
- HTTP **5xx / 429 / 408**  → :class:`~engine.core.brokers.base.BrokerConnectionError`
  (retried, then raised)
- httpx transport error      → :class:`~engine.core.brokers.base.BrokerConnectionError`
  (retried for idempotent GET/DELETE; raised immediately for non-idempotent
  ``POST .../orders`` to avoid creating duplicate orders)
- HTTP **400 / 404 / 422**   → :class:`~engine.core.brokers.base.BrokerRejectError`
  (per-order; carries the broker's error code)

The HTTP client is injectable (``client=``): production builds a real
``AsyncClient`` lazily on first use; tests inject a
``httpx.MockTransport``-backed client and exercise the full request →
response → typed-error path without touching the network.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import httpx
import structlog

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
)
from engine.core.brokers.models import BrokerPosition

logger = structlog.get_logger()

#: Local Client Portal Gateway (the common dev / paper path). Default so a
#: misconfiguration never accidentally routes a real-money cloud order.
GATEWAY_BASE_URL = "https://localhost:5000/v1/api"
#: IBKR Cloud API (OAuth2 bearer-token) — opt in explicitly for production.
CLOUD_BASE_URL = "https://api.ibkr.com/v1/api"

#: Per-request timeout (seconds).
DEFAULT_TIMEOUT_S = 10.0
#: Maximum HTTP attempts (including the first) for a retryable response.
DEFAULT_MAX_RETRIES = 3
#: Base for exponential backoff between retries: ``backoff * 2 ** attempt``.
DEFAULT_RETRY_BACKOFF_S = 0.05

#: Transient HTTP statuses → retry then raise BrokerConnectionError.
RETRY_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Auth-failure statuses → BrokerAuthError (never retried).
AUTH_STATUS: frozenset[int] = frozenset({401, 403})
#: First HTTP status we treat as a per-request rejection.
MIN_REJECTION_STATUS = 400

#: Engine-side → IBKR order-type mapping (IBKR enum is upper-case).
_ORDER_TYPE_MAP: dict[str, str] = {
    "market": "MKT",
    "mkt": "MKT",
    "limit": "LMT",
    "lmt": "LMT",
    "stop": "STP",
    "stp": "STP",
    "stop_limit": "STP_LMT",
    "stop-limit": "STP_LMT",
    "stp_lmt": "STP_LMT",
}
#: Engine-side → IBKR side mapping (IBKR enum is upper-case).
_SIDE_MAP: dict[str, str] = {
    "buy": "BUY",
    "sell": "SELL",
    "long": "BUY",
    "short": "SELL",
}

__all__ = ["IBKRBrokerAdapter"]


class IBKRBrokerAdapter:
    """Interactive Brokers REST broker adapter.

    Parameters
    ----------
    account_id:
        IBKR account id (e.g. ``"U1234567"``). **Required** — every IBKR
        order / portfolio endpoint is account-scoped.
    session_token:
        Optional bearer token (IBKR Cloud API). When set it is sent per
        request as ``Authorization: Bearer <token>``. When omitted the
        adapter relies on whatever session cookies the injected client
        already carries (the local Client Portal Gateway flow).
    base_url:
        IBKR Web API base URL. Defaults to the local gateway endpoint so a
        misconfiguration never accidentally routes a real-money cloud order.
    client:
        Optional pre-built ``httpx.AsyncClient`` (e.g. a
        ``MockTransport``-backed client in tests). When omitted, a real
        ``AsyncClient`` is created lazily on first request.
    max_retries, retry_backoff_s:
        Retry tuning for transient HTTP statuses / transport errors.
    """

    def __init__(
        self,
        account_id: str,
        *,
        session_token: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
    ) -> None:
        if not account_id or not str(account_id).strip():
            raise ValueError("IBKRBrokerAdapter requires account_id")
        self.account_id = str(account_id).strip()
        self.session_token = session_token or None
        self.base_url = base_url or GATEWAY_BASE_URL
        # An injected client (tests) overrides everything; otherwise we lazily
        # build a real AsyncClient on first use so construction never blocks.
        self._client = client
        self._owns_client = client is None  # we only aclose() a client we built
        self._max_retries = max(1, max_retries)
        self._retry_backoff_s = retry_backoff_s

        # Lifecycle state.
        self._connected = False

    # ------------------------------------------------------------------
    # Identity + lifecycle
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Stable lower-case broker identifier (``"ibkr"``)."""
        return "ibkr"

    @property
    def is_started(self) -> bool:
        """``True`` once :meth:`connect` has succeeded and not been torn down."""
        return self._connected

    async def connect(self) -> None:
        """Validate the session via ``GET /v1/api/auth/status``.

        A successful response with ``authenticated == true`` means the
        session is live and the API is reachable. A ``401/403`` (or a
        ``200`` that reports ``authenticated: false``) surfaces as
        :class:`BrokerAuthError` so the caller can engage the kill-switch
        before submitting real orders.
        """
        resp = await self._request("GET", "/auth/status")
        body: Any = None
        try:
            body = resp.json()
        except (ValueError, httpx.DecodingError):
            body = None
        authenticated = bool(isinstance(body, dict) and body.get("authenticated"))
        if not authenticated:
            raise BrokerAuthError("IBKR session reports authenticated=false")
        self._connected = True
        logger.info("ibkr.connected", account_id=self.account_id, base_url=self.base_url)

    async def disconnect(self) -> None:
        """Release the underlying http client (idempotent)."""
        client = self._client
        self._client = None
        self._connected = False
        if client is not None and self._owns_client:
            await client.aclose()
        logger.info("ibkr.disconnected")

    async def close(self) -> None:
        """Alias for :meth:`disconnect`."""
        await self.disconnect()

    # ------------------------------------------------------------------
    # Broker-direct surface
    # ------------------------------------------------------------------

    async def resolve_conid(self, symbol: str) -> int:
        """Resolve a ticker symbol to an IBKR contract id (``conid``).

        Uses ``GET /v1/api/iserver/secdef/search?symbol=...`` and returns the
        ``conid`` of the first result. Raises
        :class:`~engine.core.brokers.base.BrokerRejectError` if the symbol is
        unknown (no results) so the caller gets a clear, typed failure
        instead of placing an order against an empty contract.
        """
        symbol_clean = str(symbol).strip().upper()
        if not symbol_clean:
            raise ValueError("symbol must be non-empty")
        resp = await self._request(
            "GET", "/iserver/secdef/search", params={"symbol": symbol_clean}
        )
        try:
            results = resp.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise BrokerConnectionError(f"unparseable conid search response: {exc!s}") from exc
        if not isinstance(results, list) or not results:
            raise BrokerRejectError(
                f"unknown symbol {symbol_clean!r}: no IBKR contracts found",
                broker_code="UNKNOWN_SYMBOL",
            )
        first = results[0]
        conid = first.get("conid") if isinstance(first, dict) else None
        if conid is None:
            raise BrokerRejectError(
                f"symbol {symbol_clean!r} returned a contract without a conid",
                broker_code="UNKNOWN_SYMBOL",
            )
        return int(conid)

    async def place_order(
        self,
        symbol: str | None = None,
        qty: float | Decimal | None = None,
        side: str = "buy",
        order_type: str = "market",
        *,
        conid: int | None = None,
        limit_price: float | Decimal | None = None,
        time_in_force: str = "DAY",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit an order via ``POST /v1/api/iserver/account/{acct}/orders``.

        Translates the engine's neutral order shape into IBKR's contract-
        scoped order array. Either ``conid`` or ``symbol`` must be provided;
        when only ``symbol`` is given the adapter resolves the ``conid``
        first via :meth:`resolve_conid` so callers can work in tickers.

        Field mapping (engine → IBKR):

        ==================  ============================
        engine ``order_type``  IBKR ``orderType``
        ``market``          ``MKT``
        ``limit``           ``LMT``
        ``stop``            ``STP``
        ``stop_limit``      ``STP_LMT``
        engine ``side``       IBKR ``side``
        ``buy`` / ``long``  ``BUY``
        ``sell`` / ``short`` ``SELL``
        ==================  ============================

        Returns
        -------
        dict
            The first order item from IBKR's response (``order_id``,
            ``order_status``, …).

        Raises
        ------
        ValueError
            No ``conid``/``symbol`` given, or a limit order without
            ``limit_price``.
        BrokerAuthError
            Credentials / session rejected.
        BrokerRejectError
            The broker accepted the request but rejected the order
            (insufficient funds, unknown contract, bad price, …).
        BrokerConnectionError
            Transient / network failure after retries are exhausted
            (transport errors on the non-idempotent POST are *not* retried).
        """
        # Resolve the contract: prefer an explicit conid, else map from symbol.
        if conid is None:
            if not symbol:
                raise ValueError("place_order requires either conid or symbol")
            conid = await self.resolve_conid(symbol)
        if qty is None:
            raise ValueError("place_order requires qty")

        ibkr_type = _map_order_type(order_type)
        ibkr_side = _map_side(side)
        if ibkr_type == "LMT" and limit_price is None:
            raise ValueError("limit order requires a limit_price")

        # IBKR expects the order wrapped in an array under "orders". A
        # client order id makes the order idempotent on the broker side.
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())
        order: dict[str, Any] = {
            "conid": int(conid),
            "side": ibkr_side,
            "quantity": _format_qty(qty),
            "orderType": ibkr_type,
            "tif": str(time_in_force).upper(),
            "cOID": client_order_id,
        }
        if limit_price is not None:
            # IBKR's limit-price field is "price"; stops use "auxPrice".
            order["price"] = _format_price(limit_price)
        if ibkr_type in {"STP", "STP_LMT"} and limit_price is not None:
            order["auxPrice"] = _format_price(limit_price)

        path = f"/iserver/account/{self.account_id}/orders"
        resp = await self._request("POST", path, json={"orders": [order]})

        parsed = _parse_order_response(resp)
        logger.info(
            "ibkr.order_submitted",
            broker_order_id=parsed.get("order_id"),
            conid=order["conid"],
            side=ibkr_side,
            order_type=ibkr_type,
        )
        return parsed

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open order via ``DELETE /iserver/account/{acct}/order/{id}``.

        Raises :class:`~engine.core.brokers.base.BrokerRejectError` if the
        order is unknown or already terminal (broker returns ``404``).
        """
        path = f"/iserver/account/{self.account_id}/order/{order_id}"
        await self._request("DELETE", path)
        logger.info("ibkr.order_cancelled", broker_order_id=order_id)

    async def get_positions(self) -> list[BrokerPosition]:
        """Fetch all held positions via ``GET /portfolio/{acct}/positions``.

        Translates IBKR's position shape (signed ``position``, ``avgPrice``,
        ``mktValue``, ``unrealizedPnl``) into the broker-neutral
        :class:`~engine.core.brokers.models.BrokerPosition` so downstream
        code stays broker-agnostic. A long position has positive quantity
        and side ``"long"``; a short is reported with a positive quantity
        and side ``"short"``.
        """
        path = f"/portfolio/{self.account_id}/positions"
        resp = await self._request("GET", path)
        try:
            items = resp.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise BrokerConnectionError(f"unparseable positions response: {exc!s}") from exc
        if not isinstance(items, list):
            raise BrokerConnectionError(
                f"unexpected positions payload shape: {type(items).__name__}"
            )
        return [_position_from_ibkr(item) for item in items if isinstance(item, dict)]

    async def get_account(self) -> dict[str, Any]:
        """Fetch the account summary via ``GET /portfolio/{acct}/summary``.

        Returns the parsed summary JSON verbatim. IBKR nests each metric
        under a key whose value is ``{"amount": "...", "currency": "..."}``;
        callers that need a scalar pull ``.amount`` themselves.
        """
        path = f"/portfolio/{self.account_id}/summary"
        resp = await self._request("GET", path)
        return resp.json()

    # ------------------------------------------------------------------
    # Transport + error mapping
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Bearer-token auth header (IBKR Cloud API). Empty when relying on
        gateway cookies so we don't override the client's cookie jar."""
        if self.session_token:
            return {"Authorization": f"Bearer {self.session_token}"}
        return {}

    def _resolve_client(self) -> httpx.AsyncClient:
        """Return the active client, lazily building a real one on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=DEFAULT_TIMEOUT_S,
            )
            self._owns_client = True
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue an HTTP request with retry + typed error translation.

        - Transient HTTP statuses (``RETRY_STATUS``) and httpx transport
          errors are retried up to ``max_retries`` times with exponential
          backoff, then surfaced as :class:`BrokerConnectionError`.
        - Order submission (``POST .../orders``) is non-idempotent: a
          transport failure leaves us unable to tell whether the broker
          received the order, so retrying risks creating a duplicate. Raise
          immediately instead of retrying.
        - Auth failures (``AUTH_STATUS``) and per-order rejections (other
          4xx) are not retried — they are deterministic.
        """
        client = self._resolve_client()
        headers = self._auth_headers()
        last_exc: Exception | None = None
        non_idempotent = method.upper() == "POST" and "/orders" in path

        for attempt in range(self._max_retries):
            try:
                resp = await client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except (httpx.TransportError, httpx.RequestError) as exc:
                last_exc = exc
                if non_idempotent:
                    raise BrokerConnectionError(
                        f"transport error on non-idempotent {method} {path} "
                        f"— not retried to avoid duplicate orders ({exc!s})"
                    ) from exc
                logger.warning(
                    "ibkr.transport_error",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code in AUTH_STATUS:
                raise BrokerAuthError(f"IBKR authentication rejected (HTTP {resp.status_code})")

            if resp.status_code in RETRY_STATUS:
                last_exc = BrokerConnectionError(
                    f"IBKR transient HTTP {resp.status_code} for {method} {path}"
                )
                logger.warning(
                    "ibkr.transient_status",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    status=resp.status_code,
                )
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code >= MIN_REJECTION_STATUS:
                raise self._rejection_for(resp, method=method, path=path)

            return resp

        # Retries exhausted.
        raise BrokerConnectionError(
            f"IBKR request failed after {self._max_retries} attempts: "
            f"{method} {path} ({last_exc!s})"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        if self._retry_backoff_s <= 0:
            return
        delay = self._retry_backoff_s * (2**attempt)
        await asyncio.sleep(delay)

    @staticmethod
    def _rejection_for(resp: httpx.Response, *, method: str, path: str) -> BrokerRejectError:
        """Translate a 4xx rejection into :class:`BrokerRejectError`.

        IBKR error bodies vary: order replies use ``{"error": "..."}``,
        the search/position endpoints use ``{"message": "..."}`` or
        ``{"error": "...", "code": "..."}``. We surface whichever field is
        present as the message and ``code`` (string or int) as
        ``broker_code`` so the OMS can log the exact rejection reason.
        """
        broker_code: str | None = None
        message = f"IBKR rejected {method} {path} (HTTP {resp.status_code})"
        try:
            body = resp.json()
        except (ValueError, httpx.DecodingError):
            body = None
        if isinstance(body, dict):
            if body.get("code") is not None:
                broker_code = str(body["code"])
            # IBKR order rejections nest the human text under "error"; the
            # portal endpoints also use "message".
            text = body.get("error") or body.get("message")
            if text:
                message = str(text)
            # IBKR order replies sometimes carry a per-item error list.
            elif isinstance(body.get("message"), list) and body["message"]:
                message = str(body["message"][0])
        return BrokerRejectError(message, broker_code=broker_code)


# ---------------------------------------------------------------------------
# Module-level mapping / formatting helpers (kept private to the module).
# ---------------------------------------------------------------------------


def _map_order_type(order_type: Any) -> str:
    """Normalise an engine order type to an IBKR upper-case ``orderType``."""
    key = str(order_type).strip().lower()
    try:
        return _ORDER_TYPE_MAP[key]
    except KeyError as exc:
        raise ValueError(f"unsupported IBKR order type: {order_type!r}") from exc


def _map_side(side: Any) -> str:
    """Normalise an engine side to an IBKR upper-case ``BUY`` / ``SELL``."""
    value = getattr(side, "value", side)
    key = str(value).strip().lower()
    try:
        return _SIDE_MAP[key]
    except KeyError as exc:
        raise ValueError(f"unsupported IBKR side: {side!r}") from exc


def _format_qty(qty: Any) -> str:
    """Serialise a quantity to a numeric string for the order body."""
    if isinstance(qty, Decimal):
        return format(qty, "f")
    return str(qty)


def _format_price(price: Any) -> str:
    """Serialise a price to a clean decimal string for the order body."""
    if isinstance(price, Decimal):
        return format(price.normalize(), "f")
    return str(price)


def _parse_order_response(resp: httpx.Response) -> dict[str, Any]:
    """Extract the first order item from an IBKR place-order response.

    IBKR returns a JSON list of per-order result dicts, e.g.
    ``[{"order_id": 1234, "order_status": "Submitted"}]``. Some error
    replies come back as a single object (``{"error": "..."}``) — those are
    surfaced via :meth:`_rejection_for` on the non-2xx path, so here we only
    handle the success shape.
    """
    try:
        body = resp.json()
    except (ValueError, httpx.DecodingError) as exc:
        raise BrokerConnectionError(f"unparseable IBKR order response: {exc!s}") from exc
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0]
    if isinstance(body, dict):
        return body
    raise BrokerConnectionError(
        f"unexpected IBKR order response shape: {type(body).__name__}"
    )


def _position_from_ibkr(item: dict[str, Any]) -> BrokerPosition:
    """Translate an IBKR position object into a broker-neutral ``BrokerPosition``.

    IBKR reports a *signed* ``position`` (positive for long, negative for
    short); we report the magnitude plus an explicit ``side`` so the rest of
    the engine treats IBKR like every other broker.
    """
    raw_qty = _to_decimal(item.get("position"))
    qty = abs(raw_qty)
    side = "long" if raw_qty >= 0 else "short"
    symbol = str(
        item.get("ticker") or item.get("contractDesc") or item.get("conid") or ""
    )
    return BrokerPosition(
        symbol=symbol,
        qty=qty,
        side=side,
        avg_entry_price=_to_decimal(item.get("avgPrice")),
        market_value=_to_decimal(item.get("mktValue")),
        unrealized_pl=_to_decimal(item.get("unrealizedPnl")),
    )


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Best-effort decimal coercion that tolerates ``None`` / bad strings."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return default
