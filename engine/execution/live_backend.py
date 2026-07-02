"""Alpaca-compatible live execution backend (SEV-223).

:class:`LiveExecutionBackend` is the concrete live-trading adapter that
implements the :class:`~engine.core.execution.base.ExecutionBackend` ABC.
It routes orders to a real broker over an Alpaca-compatible REST API using
an :class:`httpx.AsyncClient`, and exposes three broker-direct async helpers:

- :meth:`submit_order`   — ``POST /v2/orders``
- :meth:`cancel_order`   — ``DELETE /v2/orders/{order_id}``
- :meth:`get_order_status` — ``GET /v2/orders/{order_id}``

Design notes
------------
* **Injectable transport.** The ``httpx.AsyncClient`` is injectable
  (``client=``) so unit tests swap in a ``httpx.MockTransport``-backed client
  and never touch the network. In production a real ``AsyncClient`` is created
  lazily on first use so construction is cheap and side-effect free. This is
  the same pattern :mod:`engine.core.brokers.alpaca` and
  :mod:`engine.data.providers.alpaca_data` use.

* **Typed error vocabulary.** HTTP failures are translated to the broker
  error hierarchy from :mod:`engine.core.brokers.base` so the live loop and
  OMS can react to auth failures, transient blips, and per-order rejections
  differently:

  - HTTP **401 / 403**        → :class:`BrokerAuthError`       (permanent; kill-switch)
  - HTTP **5xx / 429 / 408**  → :class:`BrokerConnectionError` (retried, then raised)
  - httpx transport error      → :class:`BrokerConnectionError` (retried)
  - HTTP **400 / 404 / 422**   → :class:`BrokerRejectError`     (per-order)

* **ABC conformance.** ``execute`` is implemented on top of ``submit_order``
  so the order manager can call it uniformly across backtest / paper / live
  backends; the broker-direct helpers remain available for components that
  need finer control (status polling, explicit cancellation).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
)
from engine.core.execution.base import ExecutionBackend, FillResult

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order

logger = structlog.get_logger()

#: Alpaca paper-trading base URL (default — never routes real money).
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
#: Alpaca live (real-money) base URL.
LIVE_BASE_URL = "https://api.alpaca.markets"

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


class LiveExecutionBackend(ExecutionBackend):
    """Live execution backend backed by an Alpaca-compatible REST API.

    Parameters
    ----------
    api_key, api_secret:
        Broker API credentials. Required to ``connect`` and to issue any
        order request. Both are sent per-request as the ``APCA-API-KEY-ID``
        / ``APCA-API-SECRET-KEY`` headers.
    base_url:
        Broker REST base URL. Defaults to the Alpaca **paper** endpoint so a
        misconfiguration can never accidentally route a real-money order.
    paper:
        When ``True`` (default) the backend is flagged as paper trading. Only
        honoured to pick the default ``base_url`` when one is not supplied.
    client:
        Optional pre-built ``httpx.AsyncClient`` (e.g. a
        ``MockTransport``-backed client in tests). When omitted, a real
        ``AsyncClient`` is created lazily on first request.
    max_retries, retry_backoff_s:
        Retry tuning for transient HTTP statuses / transport errors.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str | None = None,
        paper: bool = True,
        client: httpx.AsyncClient | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("LiveExecutionBackend requires api_key and api_secret")
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper = paper
        self.base_url = base_url or (PAPER_BASE_URL if paper else LIVE_BASE_URL)
        self._client = client
        self._owns_client = client is None  # we only aclose() a client we built
        self._max_retries = max(1, max_retries)
        self._retry_backoff_s = retry_backoff_s

        # ABC state.
        self._connected = False
        self._connected_at: float | None = None

    # ------------------------------------------------------------------
    # Auth + transport helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Alpaca-style auth headers, attached per request."""
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

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
        - Auth failures (``AUTH_STATUS``) and per-order rejections (other
          4xx) are not retried — they are deterministic.
        """
        client = self._resolve_client()
        headers = self._auth_headers()
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                resp = await client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except (httpx.TransportError, httpx.RequestError) as exc:
                last_exc = exc
                # Order submission (POST /v2/orders) is non-idempotent: a
                # transport failure leaves us unable to tell whether the
                # broker received the order, so retrying risks creating a
                # duplicate order. Raise immediately instead of retrying.
                if method.upper() == "POST" and "/v2/orders" in path:
                    raise BrokerConnectionError(
                        f"transport error on non-idempotent {method} {path} "
                        f"— not retried to avoid duplicate orders ({exc!s})"
                    ) from exc
                logger.warning(
                    "live_backend.transport_error",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code in AUTH_STATUS:
                raise BrokerAuthError(f"broker authentication rejected (HTTP {resp.status_code})")

            if resp.status_code in RETRY_STATUS:
                last_exc = BrokerConnectionError(
                    f"broker transient HTTP {resp.status_code} for {method} {path}"
                )
                logger.warning(
                    "live_backend.transient_status",
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
            f"broker request failed after {self._max_retries} attempts: "
            f"{method} {path} ({last_exc!s})"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        if self._retry_backoff_s <= 0:
            return
        delay = self._retry_backoff_s * (2**attempt)
        await asyncio.sleep(delay)

    @staticmethod
    def _rejection_for(resp: httpx.Response, *, method: str, path: str) -> BrokerRejectError:
        """Translate a 4xx/5xx order rejection into BrokerRejectError.

        Alpaca error bodies look like
        ``{"code": 4221000, "message": "insufficient buying power"}``.
        We surface ``code`` as ``broker_code`` and ``message`` as the text.
        """
        broker_code: str | None = None
        message = f"broker rejected {method} {path} (HTTP {resp.status_code})"
        try:
            body = resp.json()
        except (ValueError, httpx.DecodingError):
            body = None
        if isinstance(body, dict):
            if body.get("code") is not None:
                broker_code = str(body["code"])
            if body.get("message"):
                message = str(body["message"])
        return BrokerRejectError(message, broker_code=broker_code)

    # ------------------------------------------------------------------
    # Broker-direct async helpers (Alpaca-compatible REST surface)
    # ------------------------------------------------------------------

    async def submit_order(
        self,
        symbol: str,
        qty: float | Decimal,
        side: str,
        order_type: str,
        *,
        limit_price: float | Decimal | None = None,
        time_in_force: str = "day",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit an order via ``POST /v2/orders``.

        Parameters mirror the Alpaca order body. Returns the parsed order
        JSON (which includes ``id`` and ``status``).

        Raises
        ------
        BrokerAuthError
            Credentials rejected by the broker.
        BrokerRejectError
            The broker accepted the request but rejected the order
            (insufficient buying power, bad symbol, etc.).
        BrokerConnectionError
            Transient/network failure after retries are exhausted.
        """
        payload: dict[str, str] = {
            "symbol": str(symbol).upper(),
            "side": _side_value(side),
            "type": str(order_type).lower(),
            "qty": _format_qty(qty),
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            payload["limit_price"] = _format_price(limit_price)
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id

        resp = await self._request("POST", "/v2/orders", json=payload)
        order = resp.json()
        logger.info(
            "live_backend.order_submitted",
            order_id=order.get("id"),
            symbol=payload["symbol"],
            side=payload["side"],
            qty=payload["qty"],
        )
        return order

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open order via ``DELETE /v2/orders/{order_id}``.

        Alpaca replies ``204 No Content`` on success. Raises
        :class:`BrokerRejectError` if the order is unknown / already
        terminal and the broker returns ``404``.
        """
        path = f"/v2/orders/{order_id}"
        await self._request("DELETE", path)
        logger.info("live_backend.order_cancelled", order_id=order_id)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Poll an order's status via ``GET /v2/orders/{order_id}``.

        Returns the parsed order JSON (``status``, ``filled_qty``,
        ``filled_avg_price``, …).
        """
        path = f"/v2/orders/{order_id}"
        resp = await self._request("GET", path)
        return resp.json()

    # ------------------------------------------------------------------
    # ExecutionBackend ABC implementation
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate credentials + connectivity via ``GET /v2/account``.

        A ``200`` means we are authed and the API is reachable. A ``401/403``
        surfaces as :class:`BrokerAuthError` so the caller can engage the
        kill-switch before submitting real orders.
        """
        await self._request("GET", "/v2/account")
        self._connected = True
        self._connected_at = time.monotonic()
        logger.info("live_backend.connected", paper=self.paper, base_url=self.base_url)

    async def disconnect(self) -> None:
        """Release the underlying http client (idempotent)."""
        client = self._client
        self._client = None
        self._connected = False
        self._connected_at = None
        if client is not None and self._owns_client:
            await client.aclose()
        logger.info("live_backend.disconnected")

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        """Execute an internal ``Order`` by submitting it to the broker.

        Implements the :class:`ExecutionBackend` ABC on top of
        :meth:`submit_order`. Broker errors are translated into a structured
        :class:`FillResult` failure rather than propagated, so the order
        manager's loop keeps running.
        """
        if not self._connected:
            return FillResult(success=False, reason="Live backend not connected")
        try:
            submitted = await self.submit_order(
                symbol=order.symbol,
                qty=order.quantity,
                side=order.side,
                order_type=getattr(order, "order_type", "market"),
                limit_price=getattr(order, "limit_price", None),
            )
        except BrokerAuthError as exc:
            logger.exception("live_backend.auth_error", order_id=getattr(order, "id", None))
            return FillResult(success=False, reason=f"Auth error: {exc!s}")
        except BrokerRejectError as exc:
            logger.warning(
                "live_backend.order_rejected",
                order_id=getattr(order, "id", None),
                broker_code=exc.broker_code,
            )
            return FillResult(success=False, reason=f"Rejected: {exc!s}")
        except BrokerConnectionError as exc:
            logger.exception("live_backend.connection_error", order_id=getattr(order, "id", None))
            return FillResult(success=False, reason=f"Connection error: {exc!s}")

        broker_order_id = str(submitted.get("id", ""))
        status = str(submitted.get("status", "")).lower()
        filled_qty = _to_decimal_or_zero(submitted.get("filled_qty"))
        filled_avg_price = _to_decimal_or_none(submitted.get("filled_avg_price"))

        # Treat accepted / new / partially-filled as "acknowledged, not yet a
        # full fill" → success with the quantity filled so far (0 if unfilled).
        success = status in {"new", "accepted", "partially_filled", "filled"}
        fill_price = float(filled_avg_price) if filled_avg_price is not None else 0.0
        return FillResult(
            success=success,
            price=fill_price,
            quantity=int(filled_qty),
            reason=broker_order_id,
        )


# ---------------------------------------------------------------------------
# Module-level formatting helpers (kept private to the module).
# ---------------------------------------------------------------------------


def _side_value(side: Any) -> str:
    """Normalise an order side to the Alpaca ``"buy"`` / ``"sell"`` string.

    Tolerates enums (``OrderSide.BUY``), strings, and upper/lower case.
    """
    value = getattr(side, "value", side)
    return str(value).strip().lower()


def _format_qty(qty: Any) -> str:
    """Serialise a quantity to a numeric string for the order body.

    ``Decimal`` keeps full precision; ``int``/``float`` are stringified.
    """
    if isinstance(qty, Decimal):
        return format(qty, "f")
    return str(qty)


def _format_price(price: Any) -> str:
    if isinstance(price, Decimal):
        return format(price, "f")
    return str(price)


def _to_decimal_or_zero(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return Decimal("0")


def _to_decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
