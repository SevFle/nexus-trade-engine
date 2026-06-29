"""Alpaca broker adapter (gh#136 / SEV-266).

An :class:`AlpacaTradingClient` that implements the
:class:`~engine.core.brokers.models.BrokerClient` Protocol by talking to
Alpaca's trading REST API directly over httpx — no optional ``alpaca-py``
SDK required. Going direct keeps the adapter fully unit-testable with
``httpx.MockTransport`` (the pattern :mod:`engine.data.providers.alpaca_data`
already uses) and removes a runtime dependency for the common case.

The HTTP client is injectable (``client=``) so tests swap in a
``MockTransport``-backed client; in production a real ``AsyncClient`` is
lazily created.

Error vocabulary (the OMS reacts to these differently — see
:mod:`engine.core.brokers.base`):

- HTTP **401 / 403**  → :class:`BrokerAuthError`    — permanent; kill-switch.
- HTTP **5xx / 429**  → :class:`BrokerConnectionError` — retried w/ backoff;
  raised when retries are exhausted.
- httpx transport error → :class:`BrokerConnectionError` — retried.
- HTTP **400 / 404 / 422** (order-shaped) → :class:`BrokerRejectError` —
  per-order; carries Alpaca's numeric ``broker_code`` so the OMS can log
  the exact rejection reason.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
)
from engine.core.brokers.models import (
    BrokerClock,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
)

if TYPE_CHECKING:
    from decimal import Decimal

logger = structlog.get_logger()

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"

#: Default per-request timeout (seconds).
DEFAULT_TIMEOUT_S = 10.0
#: Maximum HTTP attempts (including the first) for a retryable response.
DEFAULT_MAX_RETRIES = 3
#: Base for exponential backoff between retries: ``backoff * 2 ** attempt``.
DEFAULT_RETRY_BACKOFF_S = 0.1

#: Statuses we treat as transient → retry then raise BrokerConnectionError.
RETRY_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Statuses that mean our credentials are bad → BrokerAuthError (no retry).
AUTH_STATUS: frozenset[int] = frozenset({401, 403})

#: First HTTP status we treat as a per-request rejection (any 4xx/5xx not
#: already handled as retryable or auth).
MIN_REJECTION_STATUS = 400
#: HTTP 404 Not Found — mapped to broker_code ``NO_POSITION`` for positions.
NOT_FOUND_STATUS = 404

__all__ = ["AlpacaTradingClient"]


class AlpacaTradingClient:
    """BrokerClient adapter backed by the Alpaca trading REST API."""

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
            raise ValueError("AlpacaTradingClient requires api_key and api_secret")
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper = paper
        self.base_url = base_url or (PAPER_BASE_URL if paper else LIVE_BASE_URL)
        # An injected client (tests) overrides everything; otherwise we lazily
        # build a real AsyncClient on first use so construction never blocks.
        self._client = client
        self._max_retries = max(1, max_retries)
        self._retry_backoff_s = retry_backoff_s

    @property
    def name(self) -> str:
        return "alpaca"

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    def _resolve_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=DEFAULT_TIMEOUT_S,
            )
        return self._client

    async def connect(self) -> None:
        """Validate credentials + connectivity by hitting ``GET /v2/account``.

        A 200 means we're authed and the API is reachable; a 401/403 surfaces
        as :class:`BrokerAuthError` so the caller can engage the kill-switch
        before submitting real orders.
        """
        await self._request("GET", "/v2/account")
        logger.info("alpaca.connected", paper=self.paper, base_url=self.base_url)

    async def close(self) -> None:
        """Release the underlying http client (idempotent)."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    # ------------------------------------------------------------------
    # BrokerClient contract
    # ------------------------------------------------------------------

    async def submit_order(
        self, request: BrokerOrderRequest
    ) -> BrokerOrderStatus:
        """POST ``/v2/orders``. Returns the broker's order status snapshot."""
        resp = await self._request(
            "POST", "/v2/orders", json=request.to_payload()
        )
        return BrokerOrderStatus.from_response(resp.json())

    async def get_order(self, broker_order_id: str) -> BrokerOrderStatus:
        """GET ``/v2/orders/{id}`` — poll an order's fill status."""
        resp = await self._request("GET", f"/v2/orders/{broker_order_id}")
        return BrokerOrderStatus.from_response(resp.json())

    async def cancel_order(self, broker_order_id: str) -> None:
        """DELETE ``/v2/orders/{id}``. Alpaca replies 204 on success."""
        await self._request("DELETE", f"/v2/orders/{broker_order_id}")

    async def get_clock(self) -> BrokerClock:
        """GET ``/v2/clock`` — market open/closed + next transitions."""
        resp = await self._request("GET", "/v2/clock")
        return BrokerClock.from_response(resp.json())

    async def get_position(self, symbol: str) -> BrokerPosition:
        """GET ``/v2/positions/{symbol}`` — a single held position.

        A 404 is mapped to :class:`BrokerRejectError` with broker_code
        ``NO_POSITION`` so callers can distinguish "no position" from a
        genuine transport failure.
        """
        resp = await self._request("GET", f"/v2/positions/{symbol}")
        return BrokerPosition.from_response(resp.json())

    async def get_account(self) -> dict:
        """GET ``/v2/account`` — raw account payload."""
        resp = await self._request("GET", "/v2/account")
        return resp.json()

    # ------------------------------------------------------------------
    # Transport + error mapping
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue an HTTP request with retry + typed error translation.

        Retry policy:
          - Transient HTTP statuses (``RETRY_STATUS``) and httpx transport
            errors are retried up to ``max_retries`` times with exponential
            backoff, then surfaced as :class:`BrokerConnectionError`.
          - Auth failures (``AUTH_STATUS``) and per-order rejections (other
            4xx) are *not* retried — they're deterministic and retrying
            burns quota.
        """
        client = self._resolve_client()
        last_exc: BrokerConnectionError | Exception | None = None
        # Auth headers are attached per-request so credentials travel with
        # the call even when a caller injects their own client.
        headers = self._auth_headers()

        for attempt in range(self._max_retries):
            try:
                resp = await client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except (httpx.TransportError, httpx.RequestError) as exc:
                # Network blip / DNS / connect timeout — retryable.
                last_exc = exc
                logger.warning(
                    "alpaca.transport_error",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code in AUTH_STATUS:
                raise BrokerAuthError(
                    f"alpaca authentication rejected (HTTP {resp.status_code})"
                )

            if resp.status_code in RETRY_STATUS:
                last_exc = BrokerConnectionError(
                    f"alpaca transient HTTP {resp.status_code} for {method} {path}"
                )
                logger.warning(
                    "alpaca.transient_status",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    status=resp.status_code,
                )
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code >= MIN_REJECTION_STATUS:
                # Per-request rejection (bad order, unknown order/position, …).
                raise self._rejection_for(resp, path=path)

            return resp

        # Retries exhausted.
        raise BrokerConnectionError(
            f"alpaca request failed after {self._max_retries} attempts: "
            f"{method} {path} ({last_exc!s})"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        if self._retry_backoff_s <= 0:
            return
        delay = self._retry_backoff_s * (2**attempt)
        await asyncio.sleep(delay)

    @staticmethod
    def _rejection_for(
        resp: httpx.Response, *, path: str
    ) -> BrokerRejectError:
        """Translate a 4xx order/position rejection into BrokerRejectError.

        Alpaca error bodies look like
        ``{"code": 4221000, "message": "insufficient buying power", ...}``.
        We surface ``code`` as ``broker_code`` and ``message`` as the
        exception text so the OMS can react per-order.
        """
        code: str | None = None
        message = f"alpaca rejected {path} (HTTP {resp.status_code})"
        try:
            body = resp.json()
        except (ValueError, httpx.DecodingError):
            body = None
        if isinstance(body, dict):
            if body.get("code") is not None:
                code = str(body["code"])
            if body.get("message"):
                message = str(body["message"])
        # DELETE of an unknown order, or GET of an absent position.
        if resp.status_code == NOT_FOUND_STATUS and path.startswith("/v2/positions/"):
            code = code or "NO_POSITION"
        return BrokerRejectError(message, broker_code=code)


def _format_qty(qty: Decimal) -> str:  # pragma: no cover - kept for API parity
    return format(qty, "f")
