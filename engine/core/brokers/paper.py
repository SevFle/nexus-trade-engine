"""Paper-trading broker adapter (gh#136 follow-up).

A :class:`BrokerAdapter` that simulates a broker in-process for
backtesting / staging / development. No network, no money.

Behaviour
---------
- Market orders: ack and fully fill immediately at a price returned
  by the operator-supplied ``price_for(symbol)`` callable. If the
  callable returns ``None``, the order is rejected.
- Limit orders: ack and rest. They do not auto-fill — the caller
  invokes :meth:`PaperBroker.simulate_fill` (test helper) or extends
  the adapter with a price-tick consumer.
- Cancel: rejects if the broker order is unknown or already filled;
  otherwise emits a confirming ``CancelEvent`` and removes the
  pending order.

Events are surfaced via an async queue so the live-loop driver can
``async for ev in broker.events()`` exactly as it would for a real
broker.

What this is *not*
------------------
- A market simulator. There is no order book, slippage model, or
  partial-fill schedule. That belongs in the existing backtest
  runner; this is a thin broker stand-in.
- A latency model. Fills are instantaneous in event time.
- A funds / margin checker. ``RiskGate`` upstream is responsible
  for that.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from engine.core.brokers.base import (
    BrokerError,
    BrokerRejectError,
    SubmittedOrder,
)
from engine.core.oms.events import (
    AckEvent,
    CancelEvent,
    FillEvent,
    OrderEvent,
)
from engine.core.oms.states import OrderType
from engine.observability.metrics import MetricsBackend, get_metrics

if TYPE_CHECKING:
    from engine.core.oms.order import Order


logger = structlog.get_logger()


PriceResolver = Callable[[str], Decimal | None]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class PaperBroker:
    """In-process simulated broker."""

    def __init__(
        self,
        *,
        price_for: PriceResolver,
        name: str = "paper",
        metrics: MetricsBackend | None = None,
    ) -> None:
        if not name or not name.islower():
            raise ValueError("PaperBroker name must be a lower-case slug")
        self._name = name
        self._price_for = price_for
        self._events: asyncio.Queue[OrderEvent] = asyncio.Queue()
        # broker_order_id -> pending order metadata
        self._pending: dict[str, _Pending] = {}
        self._metrics = metrics

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

    def _emit_pending_gauge(self) -> None:
        self.metrics.gauge(
            "paper_broker.pending",
            float(len(self._pending)),
            tags={"broker": self._name},
        )

    # ------------------------------------------------------------------
    # BrokerAdapter contract
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    async def submit(self, order: Order) -> SubmittedOrder:
        broker_id = f"PAPER-{uuid.uuid4()}"
        metrics = self.metrics
        base_tags = {
            "broker": self._name,
            "order_type": order.order_type.value,
        }
        if order.order_type == OrderType.MARKET:
            price = self._price_for(order.symbol)
            if price is None or price <= 0:
                metrics.counter(
                    "paper_broker.submit",
                    tags={**base_tags, "outcome": "rejected"},
                )
                raise BrokerRejectError(
                    f"paper: no price for {order.symbol}",
                    broker_code="NO_PRICE",
                )
            now = _utcnow()
            await self._events.put(
                AckEvent(occurred_at=now, broker_order_id=broker_id)
            )
            await self._events.put(
                FillEvent(
                    occurred_at=now,
                    fill_quantity=order.quantity,
                    fill_price=price,
                    fill_id=f"FILL-{broker_id}",
                )
            )
            metrics.counter(
                "paper_broker.submit",
                tags={**base_tags, "outcome": "filled"},
            )
            logger.info(
                "paper_broker.market_filled",
                order_id=str(order.id),
                broker_order_id=broker_id,
                symbol=order.symbol,
                quantity=str(order.quantity),
                price=str(price),
            )
        else:
            # Limit / stop / stop_limit — ack and rest.
            self._pending[broker_id] = _Pending(
                oms_order_id=order.id,
                symbol=order.symbol,
                quantity=order.quantity,
                order_type=order.order_type,
            )
            await self._events.put(
                AckEvent(occurred_at=_utcnow(), broker_order_id=broker_id)
            )
            metrics.counter(
                "paper_broker.submit",
                tags={**base_tags, "outcome": "resting"},
            )
            self._emit_pending_gauge()
            logger.info(
                "paper_broker.resting",
                order_id=str(order.id),
                broker_order_id=broker_id,
                order_type=order.order_type.value,
                symbol=order.symbol,
                quantity=str(order.quantity),
            )
        return SubmittedOrder(order_id=order.id, broker_order_id=broker_id)

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        if broker_order_id not in self._pending:
            self.metrics.counter(
                "paper_broker.cancel",
                tags={"broker": self._name, "outcome": "unknown"},
            )
            raise BrokerRejectError(
                f"paper: unknown or already-filled broker order {broker_order_id!r}",
                broker_code="NOT_PENDING",
            )
        del self._pending[broker_order_id]
        await self._events.put(
            CancelEvent(occurred_at=_utcnow(), requested=False, reason="paper_cancel")
        )
        self.metrics.counter(
            "paper_broker.cancel",
            tags={"broker": self._name, "outcome": "cancelled"},
        )
        self._emit_pending_gauge()
        logger.info(
            "paper_broker.cancelled",
            order_id=str(order_id),
            broker_order_id=broker_order_id,
        )

    async def events(self) -> AsyncIterator[OrderEvent]:
        # Drain whatever is queued. The caller decides when to stop —
        # typical pattern is to wrap this in an asyncio.timeout.
        while True:
            ev = await self._events.get()
            yield ev

    # ------------------------------------------------------------------
    # Test helpers (not part of the BrokerAdapter contract)
    # ------------------------------------------------------------------

    async def simulate_fill(
        self,
        *,
        broker_order_id: str,
        fill_price: Decimal | None = None,
    ) -> None:
        """Force a resting order to fully fill. For tests / dev only."""
        metrics = self.metrics
        pending = self._pending.pop(broker_order_id, None)
        if pending is None:
            metrics.counter(
                "paper_broker.simulate_fill",
                tags={"broker": self._name, "outcome": "unknown"},
            )
            raise BrokerError(
                f"simulate_fill: no pending order {broker_order_id!r}"
            )
        price = fill_price if fill_price is not None else self._price_for(pending.symbol)
        if price is None or price <= 0:
            metrics.counter(
                "paper_broker.simulate_fill",
                tags={"broker": self._name, "outcome": "no_price"},
            )
            raise BrokerRejectError(
                f"simulate_fill: no price for {pending.symbol}",
                broker_code="NO_PRICE",
            )
        await self._events.put(
            FillEvent(
                occurred_at=_utcnow(),
                fill_quantity=pending.quantity,
                fill_price=price,
                fill_id=f"FILL-{broker_order_id}",
            )
        )
        metrics.counter(
            "paper_broker.simulate_fill",
            tags={"broker": self._name, "outcome": "filled"},
        )
        self._emit_pending_gauge()

    def pending_count(self) -> int:
        return len(self._pending)

    async def drain_events(self) -> list[OrderEvent]:
        """Pop all currently-queued events without blocking. Test-only."""
        out: list[OrderEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except asyncio.QueueEmpty:
                return out


class _Pending:
    """Internal record for a resting paper order."""

    __slots__ = ("oms_order_id", "symbol", "quantity", "order_type")

    def __init__(
        self,
        *,
        oms_order_id: uuid.UUID,
        symbol: str,
        quantity: Decimal,
        order_type: OrderType,
    ) -> None:
        self.oms_order_id = oms_order_id
        self.symbol = symbol
        self.quantity = quantity
        self.order_type = order_type
