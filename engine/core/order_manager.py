"""
Order Manager — handles the full lifecycle of an order:
Signal → Validate → Cost → Risk Check → Execute → Reconcile → Log
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

from engine.core.signal import Side, Signal
from engine.observability.tracing import get_tracer

if TYPE_CHECKING:
    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import Portfolio
    from engine.core.risk_engine import RiskEngine

logger = structlog.get_logger()
_tracer = get_tracer(__name__)


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
    ):
        self.cost_model = cost_model
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.execution_backend = None  # Set by engine based on mode
        self.pending_orders: dict[str, Order] = {}
        self.completed_orders: list[Order] = []

    def set_execution_backend(self, backend):
        """Swap execution backend: backtest, paper, or live."""
        self.execution_backend = backend
        logger.info("order_manager.backend_set", backend=type(backend).__name__)

    async def process_signal(  # noqa: PLR0915
        self, signal: Signal, market_price: float, avg_volume: int = 0
    ) -> Order:
        """
        Full signal-to-order pipeline.
        Returns the final Order with its status (filled, rejected, etc.)
        """
        with _tracer.start_as_current_span("order_manager.process_signal") as span:
            quantity = signal.quantity or self._calculate_quantity(signal, market_price)
            order = Order(
                signal_id=signal.id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                side=signal.side,
                quantity=quantity,
            )
            span.set_attribute("order.id", order.id)
            span.set_attribute("order.symbol", order.symbol)
            span.set_attribute("order.side", order.side.value)
            span.set_attribute("order.quantity", order.quantity)
            span.set_attribute("order.market_price", market_price)
            logger.info(
                "order.created",
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                qty=order.quantity,
            )

            if not self._validate_order(order, market_price):
                order.transition(OrderStatus.REJECTED, "Validation failed")
                span.set_attribute("order.status", "rejected")
                span.set_attribute("order.rejection_reason", "Validation failed")
                return order
            order.transition(OrderStatus.VALIDATED)

            with _tracer.start_as_current_span("order_manager.calculate_costs") as cost_span:
                cost_breakdown = self.cost_model.estimate_total(
                    symbol=order.symbol,
                    quantity=order.quantity,
                    price=market_price,
                    side=order.side.value,
                    avg_volume=avg_volume,
                )
                cost_span.set_attribute("cost.total", cost_breakdown.total.amount)
                cost_span.set_attribute("cost.commission", cost_breakdown.commission.amount)
                cost_span.set_attribute("cost.slippage", cost_breakdown.slippage.amount)
            order.cost_breakdown = cost_breakdown.as_dict()
            order.transition(OrderStatus.COSTED)

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
                    span.set_attribute("order.status", "risk_rejected")
                    span.set_attribute("order.rejection_reason", "cost_exceeded")
                    logger.info("order.cost_rejected", order_id=order.id, cost_pct=cost_pct)
                    return order

            with _tracer.start_as_current_span("order_manager.risk_check") as risk_span:
                risk_result = self.risk_engine.check_order(order, self.portfolio, market_price)
                risk_span.set_attribute("risk.approved", risk_result.approved)
                if not risk_result.approved:
                    risk_span.set_attribute("risk.reason", risk_result.reason)
            if not risk_result.approved:
                order.transition(OrderStatus.RISK_REJECTED, risk_result.reason)
                span.set_attribute("order.status", "risk_rejected")
                span.set_attribute("order.rejection_reason", risk_result.reason)
                logger.warn("order.risk_rejected", order_id=order.id, reason=risk_result.reason)
                return order
            order.transition(OrderStatus.RISK_APPROVED)

            if self.execution_backend is None:
                order.transition(OrderStatus.FAILED, "No execution backend configured")
                span.set_attribute("order.status", "failed")
                return order

            order.transition(OrderStatus.SUBMITTED)

            with _tracer.start_as_current_span("order_manager.execute") as exec_span:
                exec_span.set_attribute("execution.backend", type(self.execution_backend).__name__)
                fill = await self.execution_backend.execute(order, market_price, cost_breakdown)
                exec_span.set_attribute("execution.success", fill.success)
                if fill.success:
                    exec_span.set_attribute("execution.fill_price", fill.price)
                    exec_span.set_attribute("execution.fill_quantity", fill.quantity)

            if fill.success:
                order.fill_price = fill.price
                order.fill_quantity = fill.quantity
                order.filled_at = datetime.now(UTC)
                order.transition(OrderStatus.FILLED)

                total_cost = cost_breakdown.total.amount
                if order.side == Side.BUY:
                    self.portfolio.open_position(
                        order.symbol, fill.quantity,
                        fill.price, total_cost,
                    )
                elif order.side == Side.SELL:
                    tax = cost_breakdown.tax_estimate.amount
                    non_tax_cost = total_cost - tax
                    self.portfolio.close_position(
                        order.symbol, fill.quantity, fill.price, non_tax_cost, tax
                    )

                span.set_attribute("order.status", "filled")
                span.set_attribute("order.fill_price", fill.price)
                span.set_attribute("order.fill_quantity", fill.quantity)
                logger.info("order.filled", order_id=order.id, price=fill.price, qty=fill.quantity)
            else:
                order.transition(OrderStatus.FAILED, fill.reason)
                span.set_attribute("order.status", "failed")
                span.set_attribute("order.failure_reason", fill.reason)
                logger.error("order.failed", order_id=order.id, reason=fill.reason)

            self.completed_orders.append(order)
            return order

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
