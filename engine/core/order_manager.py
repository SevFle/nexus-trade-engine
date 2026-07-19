"""
Order Manager — handles the full lifecycle of an order:
Signal → Validate → Cost → Risk Check → Execute → Reconcile → Log
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

from engine.core.signal import Side, Signal
from engine.events.bus import EventType
from engine.observability.metrics import MetricsBackend, get_metrics

if TYPE_CHECKING:
    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import Portfolio
    from engine.core.risk_engine import RiskEngine
    from engine.events.bus import EventBus

logger = structlog.get_logger()


class OrderStatus(StrEnum):
    PENDING = "pending"
    VALIDATED = "validated"
    COSTED = "costed"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class Order(BaseModel):
    """Internal order representation. Created from Signals by the OrderManager."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ── Origin ──
    signal_id: str
    strategy_id: str

    # ── Trade details ──
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None

    # ── Status tracking ──
    status: OrderStatus = OrderStatus.PENDING
    status_history: list[dict] = Field(default_factory=list)

    # ── Cost & execution ──
    cost_breakdown: dict | None = None
    fill_price: float | None = None
    fill_quantity: int | None = None
    filled_at: datetime | None = None

    def transition(self, new_status: OrderStatus, reason: str = ""):
        self.status_history.append(
            {
                "from": self.status,
                "to": new_status,
                "timestamp": datetime.now(UTC).isoformat(),
                "reason": reason,
            }
        )
        self.status = new_status


class OrderManager:
    """
    Processes signals into orders through the full pipeline.

    Signal → Validate → Cost → Risk Check → Execute → Reconcile
    """

    def __init__(
        self,
        cost_model: ICostModel,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
        event_bus: EventBus | None = None,
        metrics: MetricsBackend | None = None,
    ):
        self.cost_model = cost_model
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        # Optional event bus. When wired, fill events are published so the
        # WebSocket event bridge can broadcast them to connected clients.
        self._event_bus = event_bus
        self._metrics = metrics
        self.execution_backend = None  # Set by engine based on mode
        self.pending_orders: dict[str, Order] = {}
        self.completed_orders: list[Order] = []

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process-wide singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

    def set_execution_backend(self, backend):
        """Swap execution backend: backtest, paper, or live."""
        self.execution_backend = backend
        logger.info("order_manager.backend_set", backend=type(backend).__name__)

    async def process_signal(
        self, signal: Signal, market_price: float, avg_volume: int = 0
    ) -> Order:
        """
        Full signal-to-order pipeline.
        Returns the final Order with its status (filled, rejected, etc.)
        """
        # Step 1: Create order from signal
        quantity = signal.quantity or self._calculate_quantity(signal, market_price)
        order = Order(
            signal_id=signal.id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
        )
        logger.info(
            "order.created",
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=order.quantity,
        )

        # Step 2: Validate basic constraints
        if not self._validate_order(order, market_price):
            order.transition(OrderStatus.REJECTED, "Validation failed")
            return order
        order.transition(OrderStatus.VALIDATED)

        # Step 3: Calculate costs
        cost_breakdown = self.cost_model.estimate_total(
            symbol=order.symbol,
            quantity=order.quantity,
            price=market_price,
            side=order.side.value,
            avg_volume=avg_volume,
        )
        order.cost_breakdown = cost_breakdown.as_dict()
        order.transition(OrderStatus.COSTED)

        # Step 3b: Check if cost exceeds strategy's max tolerance
        if signal.max_cost_pct is not None:
            trade_value = order.quantity * market_price
            cost_pct = (
                cost_breakdown.total_without_tax.amount / trade_value
                if trade_value > 0
                else float("inf")
            )
            if cost_pct > signal.max_cost_pct:
                order.transition(
                    OrderStatus.RISK_REJECTED,
                    f"Cost {cost_pct:.4f} exceeds max {signal.max_cost_pct:.4f}",
                )
                logger.info("order.cost_rejected", order_id=order.id, cost_pct=cost_pct)
                return order

        # Step 4: Risk checks
        risk_result = self.risk_engine.check_order(order, self.portfolio, market_price)
        if not risk_result.approved:
            order.transition(OrderStatus.RISK_REJECTED, risk_result.reason)
            logger.warn("order.risk_rejected", order_id=order.id, reason=risk_result.reason)
            return order
        order.transition(OrderStatus.RISK_APPROVED)

        # Step 5: Execute
        if self.execution_backend is None:
            order.transition(OrderStatus.FAILED, "No execution backend configured")
            return order

        order.transition(OrderStatus.SUBMITTED)
        fill = await self.execution_backend.execute(order, market_price, cost_breakdown)

        # Step 6: Reconcile
        if fill.success:
            order.fill_price = fill.price
            order.fill_quantity = fill.quantity
            order.filled_at = datetime.now(UTC)
            order.transition(OrderStatus.FILLED)

            # Update portfolio
            total_cost = cost_breakdown.total.amount
            if order.side == Side.BUY:
                self.portfolio.open_position(order.symbol, fill.quantity, fill.price, total_cost)
            elif order.side == Side.SELL:
                tax = cost_breakdown.tax_estimate.amount
                non_tax_cost = total_cost - tax
                self.portfolio.close_position(
                    order.symbol, fill.quantity, fill.price, non_tax_cost, tax
                )

            logger.info("order.filled", order_id=order.id, price=fill.price, qty=fill.quantity)
        else:
            order.transition(OrderStatus.FAILED, fill.reason)
            logger.error("order.failed", order_id=order.id, reason=fill.reason)

        # Record the order BEFORE publishing the fill event so a publish
        # failure — whether swallowed (infrastructure) or propagated
        # (programmer bug) — never loses it from the completed-orders log
        # that downstream reconciliation relies on.
        self.completed_orders.append(order)
        if order.status == OrderStatus.FILLED:
            await self._publish_fill_event(order)
        return order

    async def _publish_fill_event(self, order: Order) -> None:
        """Publish an ``ORDER_FILLED`` event to the event bus.

        The :class:`~engine.api.ws.event_bridge.EventBusBridge` subscribes
        to ``ORDER_FILLED`` and fans the event out to WebSocket clients on
        the ``orders`` channel, giving connected clients real-time order
        status updates.

        Publishing is best-effort for *infrastructure* failures
        (:class:`ConnectionError`, :class:`TimeoutError`, and
        :class:`RuntimeError` — the errors the bus itself raises when
        Redis or the in-process dispatcher is unavailable): such failures
        are logged, a ``order_manager.fill_event_publish_failed`` counter
        is incremented, and order execution continues uninterrupted.

        Any *other* exception (e.g. :class:`TypeError` from a programmer
        bug, or :class:`ValueError` from a malformed payload) is
        intentionally re-raised so it surfaces during development instead
        of being silently swallowed.
        """
        if self._event_bus is None:
            return
        payload = {
            "order_id": order.id,
            "symbol": order.symbol,
            "side": order.side.value,
            "qty": order.fill_quantity,
            "price": order.fill_price,
            "timestamp": order.filled_at.isoformat() if order.filled_at else None,
            "status": order.status.value,
            "strategy_id": order.strategy_id,
            "signal_id": order.signal_id,
        }
        try:
            # Bound how long we wait on the bus: a wedged Redis or
            # in-process dispatcher must never stall order execution.
            # ``asyncio.wait_for`` raises ``TimeoutError`` on expiry,
            # which the clause below treats as best-effort infrastructure
            # failure alongside the bus' own transport errors.
            await asyncio.wait_for(
                self._event_bus.emit(
                    EventType.ORDER_FILLED, payload, source="order_manager"
                ),
                timeout=2.0,
            )
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            # Bus / transport infrastructure failures are best-effort:
            # log + metric, then keep processing orders. A WebSocket/outbox
            # outage must never break order execution.
            self.metrics.counter(
                "order_manager.fill_event_publish_failed",
                tags={"error_type": type(exc).__name__},
            )
            logger.exception(
                "order_manager.fill_event_publish_failed",
                order_id=order.id,
                error_type=type(exc).__name__,
            )

    def _calculate_quantity(self, signal: Signal, price: float) -> int:
        """Convert signal weight to share quantity."""
        if price <= 0:
            return 0
        available = self.portfolio.cash * signal.weight
        return int(available // price)

    def _validate_order(self, order: Order, price: float) -> bool:
        """Basic order validation."""
        if order.quantity <= 0:
            return False
        if order.side == Side.BUY:
            required_cash = order.quantity * price
            if required_cash > self.portfolio.cash:
                return False
        elif order.side == Side.SELL:
            pos = self.portfolio.positions.get(order.symbol)
            if not pos or pos.quantity < order.quantity:
                return False
        return True
