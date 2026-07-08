"""Alpaca broker adapter (SEV-223+).

A thin, broker-facing adapter (:class:`AlpacaBrokerAdapter`) that sits on
top of the existing :class:`~engine.execution.live_backend.LiveExecutionBackend`
— an :class:`~engine.core.execution.base.ExecutionBackend`. It exposes the
broker-direct REST surface the engine expects from an Alpaca integration:

- :meth:`AlpacaBrokerAdapter.submit_order` — ``POST /v2/orders`` (market + limit)
- :meth:`AlpacaBrokerAdapter.cancel_order` — ``DELETE /v2/orders/{id}``
- :meth:`AlpacaBrokerAdapter.get_order_status` — ``GET /v2/orders/{id}``
- :meth:`AlpacaBrokerAdapter.get_position` — ``GET /v2/positions/{symbol}``

The adapter deliberately does **not** reimplement any HTTP, auth, retry, or
error-mapping logic. All of that lives in exactly one place —
``LiveExecutionBackend._request`` — and the adapter delegates to it. This
keeps the auth headers (``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``), the
exponential backoff, and the typed failure vocabulary from
:mod:`engine.core.brokers.base` uniform across every Alpaca entry point:

- HTTP **401 / 403** → :class:`~engine.core.brokers.base.BrokerAuthError`
- HTTP **5xx / 429 / 408** → :class:`~engine.core.brokers.base.BrokerConnectionError`
  (retried, then raised)
- httpx transport error → :class:`~engine.core.brokers.base.BrokerConnectionError`
- HTTP **400 / 404 / 422** → :class:`~engine.core.brokers.base.BrokerRejectError`

The adapter owns no ``httpx.AsyncClient`` of its own: whatever client is
injected (``client=``) is handed straight to the backend, so a
``httpx.MockTransport``-backed client in tests exercises the full
request → response → typed-error path end to end without touching the
network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from engine.core.brokers.models import BrokerPosition
from engine.execution.live_backend import LiveExecutionBackend

if TYPE_CHECKING:
    from decimal import Decimal

    import httpx

logger = structlog.get_logger()

__all__ = ["AlpacaBrokerAdapter"]


class AlpacaBrokerAdapter:
    """Alpaca REST broker adapter built on :class:`LiveExecutionBackend`.

    Parameters
    ----------
    api_key, api_secret:
        Alpaca API credentials. Sent per request as the
        ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers (handled by
        the backend).
    base_url:
        Broker REST base URL. Defaults to the Alpaca **paper** endpoint so
        a misconfiguration can never accidentally route a real-money order.
    paper:
        When ``True`` (default) the adapter is flagged as paper trading.
        Only used to pick the default ``base_url`` when one is not supplied.
    client:
        Optional pre-built ``httpx.AsyncClient`` (e.g. a
        ``MockTransport``-backed client in tests). When omitted, a real
        ``AsyncClient`` is created lazily on first request by the backend.
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
        max_retries: int = 3,
        retry_backoff_s: float = 0.05,
    ) -> None:
        # Delegate everything transport-related to the existing backend so
        # there is a single source of truth for auth + retry + error mapping.
        self._backend = LiveExecutionBackend(
            api_key,
            api_secret,
            base_url=base_url,
            paper=paper,
            client=client,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
        )

    @property
    def name(self) -> str:
        """Stable lower-case broker identifier (``"alpaca"``)."""
        return "alpaca"

    @property
    def backend(self) -> LiveExecutionBackend:
        """The underlying execution backend (exposed for diagnostics/tests)."""
        return self._backend

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate credentials + connectivity (``GET /v2/account``).

        Raises :class:`~engine.core.brokers.base.BrokerAuthError` if the
        broker rejects the credentials, so a caller can engage the
        kill-switch before submitting real orders.
        """
        await self._backend.connect()
        logger.info("alpaca_adapter.connected", name=self.name, paper=self._backend.paper)

    async def disconnect(self) -> None:
        """Release the underlying http client (idempotent)."""
        await self._backend.disconnect()

    async def close(self) -> None:
        """Alias for :meth:`disconnect`."""
        await self.disconnect()

    # ------------------------------------------------------------------
    # Broker-direct surface
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
        """Submit a market or limit order via ``POST /v2/orders``.

        Delegates to :meth:`LiveExecutionBackend.submit_order`. The only
        adapter-local logic is normalising ``order_type`` to lower case and
        rejecting a *limit* order that is missing its ``limit_price`` up
        front — otherwise Alpaca would reject it later with a less obvious
        422 that hides the real cause.

        Parameters mirror the Alpaca order body; ``client_order_id`` is the
        broker-side idempotency key (auto-generated when omitted).

        Returns
        -------
        dict
            The parsed broker order JSON (``id``, ``status``,
            ``filled_qty``, …).

        Raises
        ------
        ValueError
            A ``limit`` order was submitted without a ``limit_price``.
        BrokerAuthError
            Credentials rejected.
        BrokerRejectError
            The broker accepted the request but rejected the order
            (insufficient buying power, unknown symbol, bad price, …).
        BrokerConnectionError
            Transient / network failure after retries are exhausted.
        """
        normalised = str(order_type).strip().lower()
        if normalised == "limit" and limit_price is None:
            raise ValueError("limit order requires a limit_price")
        order = await self._backend.submit_order(
            symbol,
            qty,
            side,
            normalised,
            limit_price=limit_price,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )
        logger.info(
            "alpaca_adapter.order_submitted",
            broker_order_id=order.get("id"),
            symbol=order.get("symbol"),
            order_type=normalised,
        )
        return order

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open order via ``DELETE /v2/orders/{order_id}``.

        Alpaca replies ``204`` on success. Raises
        :class:`~engine.core.brokers.base.BrokerRejectError` if the order is
        unknown or already terminal (broker returns ``404``).
        """
        await self._backend.cancel_order(order_id)
        logger.info("alpaca_adapter.order_cancelled", broker_order_id=order_id)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Poll an order's status via ``GET /v2/orders/{order_id}``.

        Returns the parsed broker order JSON (``status``, ``filled_qty``,
        ``filled_avg_price``, …).
        """
        return await self._backend.get_order_status(order_id)

    async def get_position(self, symbol: str) -> BrokerPosition:
        """Fetch a single held position via ``GET /v2/positions/{symbol}``.

        Reuses the backend's request pipeline (auth headers, retry, typed
        error mapping) so failures surface with the same
        :class:`~engine.core.brokers.base.BrokerError` vocabulary as order
        submission. A 404 (no open position for ``symbol``) is mapped to a
        :class:`~engine.core.brokers.base.BrokerRejectError` carrying the
        broker's numeric ``code`` so callers can distinguish "no position"
        from a genuine transport failure.

        Returns
        -------
        BrokerPosition
            Parsed position (qty, side, avg entry price, market value,
            unrealized P&L).
        """
        data = await self._backend.get_position(symbol)
        return BrokerPosition.from_response(data)
