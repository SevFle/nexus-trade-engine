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
    from engine.core.cost_model import CostBreakdown, ICostModel
    from engine.core.execution.base import FillResult
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
    # ``fill_price`` holds the volume-weighted average price (VWAP) of
    # every fill applied to this order. For a single complete fill it is
    # just that fill's price; for an order resumed via
    # :meth:`OrderManager.continue_order` it is the running VWAP across
    # all fills (initial partial + subsequent continuations).
    fill_price: float | None = None
    # ``fill_quantity`` is the *cumulative* number of shares filled so
    # far. A partial fill leaves it below ``quantity``; subsequent
    # continuations accumulate into it until it reaches ``quantity``.
    fill_quantity: int | None = None
    filled_at: datetime | None = None
    # Per-fill audit trail. Each entry is ``{price, quantity, timestamp}``.
    # Populated by ``_reconcile_fill`` as fills land — a single
    # ``process_signal`` fill appends one entry; ``continue_order``
    # appends one per continuation attempt. Lets downstream consumers
    # reconstruct the exact fill sequence that produced the cumulative
    # VWAP in ``fill_price``.
    fills: list[dict] = Field(default_factory=list)

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
            await self._reconcile_fill(order, fill, cost_breakdown)
        else:
            order.transition(OrderStatus.FAILED, fill.reason)
            logger.error("order.failed", order_id=order.id, reason=fill.reason)

        # Record the order BEFORE publishing the fill event so a publish
        # failure — whether swallowed (infrastructure) or propagated
        # (programmer bug) — never loses it from the completed-orders log
        # that downstream reconciliation relies on.
        self.completed_orders.append(order)
        # Publish a fill event for *both* complete and partial fills.
        # WebSocket clients / outbox consumers rely on the
        # ``ORDER_FILLED`` event to track order progress; gating it on
        # ``FILLED`` alone silently drops partial fills, leaving clients
        # unaware that shares changed hands.
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
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

    async def _reconcile_fill(
        self,
        order: Order,
        fill: FillResult,
        cost_breakdown: CostBreakdown,
    ) -> None:
        """Apply ``fill`` to ``order`` in place.

        Centralises the post-execution bookkeeping shared by
        :meth:`process_signal` (initial fill) and :meth:`continue_order`
        (residual fill on a partially-filled order):

        * Accumulate ``fill.quantity`` into the order's *cumulative*
          ``fill_quantity`` and recompute ``fill_price`` as the
          volume-weighted average (VWAP) across every fill applied so
          far. For an initial complete fill the VWAP degenerates to the
          fill's price, so single-fill callers see no behaviour change.
        * Append a per-fill record to ``order.fills`` for auditability.
        * Transition the order to ``PARTIALLY_FILLED`` while the
          cumulative quantity is short of ``order.quantity``, and to
          ``FILLED`` once it reaches it.
        * Update the portfolio (open/close position, adjust cash) for
          *this* fill's quantity — not the whole order — so a
          continuation fill books only the newly executed shares.

        The portfolio cost/tax split is taken from ``cost_breakdown``,
        which the caller computes for the slice being executed.
        """
        # Cumulative quantity + running VWAP. On a fresh fill both
        # prior values are None and resolve to zero, so the VWAP
        # collapses to this fill's price.
        prior_qty = order.fill_quantity or 0
        prior_vwap = order.fill_price or 0.0
        new_qty = prior_qty + fill.quantity
        if new_qty > 0:
            new_vwap = (prior_vwap * prior_qty + fill.price * fill.quantity) / new_qty
        else:
            new_vwap = fill.price
        order.fill_price = new_vwap
        order.fill_quantity = new_qty
        order.filled_at = datetime.now(UTC)
        order.fills.append(
            {
                "price": fill.price,
                "quantity": fill.quantity,
                "timestamp": order.filled_at.isoformat(),
            }
        )

        # An execution backend may fill fewer shares than requested
        # (e.g. thin liquidity, exchange rounding). Such a fill must be
        # marked ``PARTIALLY_FILLED`` so downstream consumers can
        # distinguish it from a complete fill and react accordingly
        # (e.g. resubmit the residual via ``continue_order``). Only a
        # cumulative fill that fully satisfies ``order.quantity`` counts
        # as ``FILLED``; everything else is partial.
        if order.fill_quantity < order.quantity:
            order.transition(OrderStatus.PARTIALLY_FILLED)
        else:
            order.transition(OrderStatus.FILLED)

        # Update portfolio for *this* fill only.
        total_cost = cost_breakdown.total.amount
        if order.side == Side.BUY:
            self.portfolio.open_position(order.symbol, fill.quantity, fill.price, total_cost)
        elif order.side == Side.SELL:
            tax = cost_breakdown.tax_estimate.amount
            non_tax_cost = total_cost - tax
            self.portfolio.close_position(
                order.symbol, fill.quantity, fill.price, non_tax_cost, tax
            )

        logger.info(
            "order.filled",
            order_id=order.id,
            price=fill.price,
            qty=fill.quantity,
            cumulative_qty=order.fill_quantity,
            vwap=order.fill_price,
        )

    def _find_order(self, order_id: str) -> Order | None:
        """Look up an order by id across pending and completed stores."""
        if order_id in self.pending_orders:
            return self.pending_orders[order_id]
        for order in self.completed_orders:
            if order.id == order_id:
                return order
        return None

    async def continue_order(
        self,
        order_id: str,
        market_price: float | None = None,
        max_retries: int = 10,
    ) -> Order:
        """Resume a partially-filled order by executing its residual.

        Loads the existing order by ``order_id``, asserts it is
        ``PARTIALLY_FILLED``, then submits the unfilled remainder to the
        execution backend and reconciles the resulting fill back onto
        the *same* :class:`Order` object via :meth:`_reconcile_fill`.

        If the backend again under-fills (returning fewer shares than
        the residual), the loop submits the new remainder and tries
        again — bounded by ``max_retries`` (default ``10``) so a
        pathological backend that always under-fills cannot trap the
        caller in an infinite loop. On hitting the cap the order is
        returned in whatever state the last successful reconciliation
        left it (typically still ``PARTIALLY_FILLED``).

        Args:
            order_id: Id of the order to continue.
            market_price: Reference price for costing and executing the
                residual. Defaults to the order's current VWAP
                (``fill_price``) when omitted, so callers can resume
                without re-quoting.
            max_retries: Hard cap on continuation attempts. ``<= 0`` is
                treated as ``1`` so the caller always gets at least one
                execution attempt.

        Returns:
            The same :class:`Order` object that was loaded, mutated in
            place with the cumulative ``fill_quantity``/VWAP and an
            updated status.

        Raises:
            ValueError: If no order with ``order_id`` is known, if its
                status is not ``PARTIALLY_FILLED``, or if no execution
                backend is configured.
        """
        order = self._find_order(order_id)
        if order is None:
            raise ValueError(f"Unknown order: {order_id}")
        if order.status != OrderStatus.PARTIALLY_FILLED:
            raise ValueError(
                f"Cannot continue order {order_id}: status is "
                f"{order.status.value}, expected partially_filled"
            )
        if self.execution_backend is None:
            raise ValueError("No execution backend configured")

        # Always permit at least one execution attempt.
        attempts_cap = max(1, max_retries)
        price = market_price if market_price is not None else (order.fill_price or 0.0)

        attempts = 0
        while order.status == OrderStatus.PARTIALLY_FILLED and attempts < attempts_cap:
            attempts += 1
            remaining = order.quantity - (order.fill_quantity or 0)
            if remaining <= 0:
                break

            cost_breakdown = self.cost_model.estimate_total(
                symbol=order.symbol,
                quantity=remaining,
                price=price,
                side=order.side.value,
            )
            fill = await self.execution_backend.execute(order, price, cost_breakdown)
            if not fill.success:
                order.transition(OrderStatus.FAILED, fill.reason)
                logger.error(
                    "order.continue_failed",
                    order_id=order.id,
                    attempt=attempts,
                    reason=fill.reason,
                )
                break

            await self._reconcile_fill(order, fill, cost_breakdown)
            # Publish an incremental fill event so WebSocket / outbox
            # consumers observe every continuation fill, not just the
            # initial partial fill recorded by ``process_signal``.
            await self._publish_fill_event(order)

        if attempts >= attempts_cap and order.status == OrderStatus.PARTIALLY_FILLED:
            logger.warn(
                "order.continue_max_retries_reached",
                order_id=order.id,
                attempts=attempts,
                remaining=order.quantity - (order.fill_quantity or 0),
            )

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
