"""
PaperTradeExecutionBackend — unified paper trade execution engine.

Combines fill simulation, position tracking, commission calculation,
pre-trade risk checks, event bus emission, metrics collection, and
broker adapter compatibility into a single class implementing
ExecutionBackend.  This is the primary backend for paper_trade mode
in the three-mode architecture (Backtest / Paper / Live).

Features:
- Pluggable slippage models (fixed, percentage, sqrt, volume-weighted, random)
- Pluggable commission models (per-share, flat, percentage, tiered, zero)
- Position tracking with realized/unrealized P&L (integer-cent precision)
- Pre-trade risk checks (position size, rate limit, daily loss, symbol filter)
- Partial fill simulation
- Latency simulation with jitter
- Fill statistics tracking (global and per-symbol)
- Event emission via EventBus for all order lifecycle transitions
- Metrics via MetricsBackend (counters, gauges, histograms)
- Clock abstraction (IClock) for testability
- Order modification (quantity, limit/stop price)
- Session resume from persisted state
- Broker adapter bridge (adapts to BrokerAdapter Protocol)
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.execution.base import ExecutionBackend, FillResult
from engine.core.execution.clock import IClock, SystemClock
from engine.core.execution.commission import (
    CommissionModelType,
    ICommissionCalculator,
    create_commission_calculator,
)
from engine.core.execution.paper_broker_interface import (
    OrderRejectReason,
    PaperOrderStatus,
    PaperPortfolioSnapshot,
    PaperPosition,
    PaperTradeBrokerConfig,
    PaperTradeFill,
    PaperTradeRiskConfig,
)
from engine.core.execution.position_tracker import PaperPositionTracker
from engine.core.execution.slippage import (
    SlippageContext,
    create_slippage_model,
)
from engine.events.bus import EventType as _EventType

if TYPE_CHECKING:
    from engine.events.bus import EventBus

logger = structlog.get_logger()

_PARTIAL_FILL_MAX_RATIO = 1.0


@dataclass
class _OpenOrder:
    order_id: str
    symbol: str
    side: str
    quantity: int
    original_quantity: int
    order_type: str
    limit_price: float | None
    stop_price: float | None
    status: PaperOrderStatus
    created_at: str
    filled_quantity: int = 0
    filled_price_total: float = 0.0
    commission: float = 0.0
    reject_reason: str = ""

    @property
    def avg_fill_price(self) -> float:
        if self.filled_quantity == 0:
            return 0.0
        return self.filled_price_total / self.filled_quantity


@dataclass
class _OrderRecord:
    order_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    status: PaperOrderStatus
    fill_price: float
    fill_quantity: int
    commission: float
    created_at: str
    filled_at: str | None
    reject_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "status": self.status.value,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "commission": self.commission,
            "created_at": self.created_at,
            "filled_at": self.filled_at,
            "reject_reason": self.reject_reason,
        }


@dataclass
class _RiskCheck:
    approved: bool
    reason: str = ""
    reject_reason: OrderRejectReason | None = None


@dataclass
class _FillStats:
    total_orders: int = 0
    filled_orders: int = 0
    partial_fills: int = 0
    rejected_orders: int = 0
    total_notional: float = 0.0
    total_slippage_bps: float = 0.0
    total_latency_ms: float = 0.0

    @property
    def fill_rate(self) -> float:
        return self.filled_orders / self.total_orders if self.total_orders > 0 else 0.0

    @property
    def avg_slippage_bps(self) -> float:
        return self.total_slippage_bps / self.filled_orders if self.filled_orders > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.filled_orders if self.filled_orders > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_orders": self.total_orders,
            "filled_orders": self.filled_orders,
            "partial_fills": self.partial_fills,
            "rejected_orders": self.rejected_orders,
            "fill_rate": round(self.fill_rate, 4),
            "total_notional": round(self.total_notional, 2),
            "avg_slippage_bps": round(self.avg_slippage_bps, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
        }


class PaperTradeExecutionBackend(ExecutionBackend):
    """
    Full-featured paper trade execution backend.

    Implements ExecutionBackend for OrderManager integration, provides
    IPaperTradeBroker-style methods for direct use, emits events to
    EventBus, and collects metrics via MetricsBackend.
    """

    def __init__(
        self,
        config: PaperTradeBrokerConfig | None = None,
        *,
        initial_cash: float = 100_000.0,
        data_provider: Any = None,
        commission_calculator: ICommissionCalculator | None = None,
        event_bus: EventBus | None = None,
        clock: IClock | None = None,
        metrics: Any = None,
    ) -> None:
        self._config = config or PaperTradeBrokerConfig()
        self._data_provider = data_provider
        self._rng = random.Random(self._config.random_seed)  # noqa: S311
        self._connected = False
        self._clock = clock or SystemClock()
        self._event_bus = event_bus
        self._metrics = metrics

        self._slippage = create_slippage_model(
            self._config.slippage_model_type,
            **(self._config.slippage_model_kwargs or {}),
        )

        if commission_calculator is not None:
            self._commission = commission_calculator
        else:
            self._commission = create_commission_calculator(
                CommissionModelType.PER_SHARE,
                rate_per_share=self._config.commission_per_share,
                min_commission=self._config.min_commission,
            )

        self._tracker = PaperPositionTracker(initial_cash=initial_cash)
        self._risk_config = self._config.risk_config or PaperTradeRiskConfig()
        self._initial_equity = initial_cash

        self._open_orders: dict[str, _OpenOrder] = {}
        self._order_history: list[_OrderRecord] = []
        self._market_prices: dict[str, float] = {}
        self._fills: list[PaperTradeFill] = []

        self._order_timestamps: list[float] = []
        self._session_start: float | None = None

        self._stats = _FillStats()
        self._per_symbol_stats: dict[str, _FillStats] = defaultdict(_FillStats)
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def config(self) -> PaperTradeBrokerConfig:
        return self._config

    @property
    def clock(self) -> IClock:
        return self._clock

    @property
    def position_tracker(self) -> PaperPositionTracker:
        return self._tracker

    @property
    def stats(self) -> _FillStats:
        return self._stats

    def get_symbol_stats(self, symbol: str) -> _FillStats:
        return self._per_symbol_stats.get(symbol, _FillStats())

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            et = _EventType(event_type)
            try:
                loop = asyncio.get_running_loop()
                _task = loop.create_task(
                    self._event_bus.emit(et, data, source="paper_backend")
                )
                self._background_tasks.append(_task)
                _task.add_done_callback(self._background_tasks.discard)
            except RuntimeError:
                pass
        except Exception:
            logger.debug(
                "paper_backend.event_emit_failed", event_type=event_type
            )

    def _emit_metric_counter(
        self, name: str, value: float = 1.0,
        tags: dict[str, str] | None = None,
    ) -> None:
        if self._metrics is not None:
            self._metrics.counter(name, value, tags=tags)

    def _emit_metric_gauge(
        self, name: str, value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        if self._metrics is not None:
            self._metrics.gauge(name, value, tags=tags)

    def _emit_metric_histogram(
        self, name: str, value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        if self._metrics is not None:
            self._metrics.histogram(name, value, tags=tags)

    async def connect(self) -> None:
        self._connected = True
        self._session_start = self._clock.monotonic()
        self._emit_event("paper.session.started", {})
        logger.info(
            "paper_backend.connected",
            fill_probability=self._config.fill_probability,
            slippage_model=type(self._slippage).__name__,
            commission_model=type(self._commission).__name__,
            latency_ms=self._config.latency_ms,
        )

    async def disconnect(self) -> None:
        if self._connected:
            for order_id in list(self._open_orders):
                order = self._open_orders.pop(order_id)
                now = self._now_iso()
                self._order_history.append(_OrderRecord(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    order_type=order.order_type,
                    status=PaperOrderStatus.CANCELLED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=0.0,
                    created_at=order.created_at,
                    filled_at=now,
                    reject_reason="Session shutdown",
                ))
                self._emit_event(
                    "paper.order.cancelled",
                    {"order_id": order_id, "reason": "Session shutdown"},
                )
        self._connected = False
        snapshot = self._tracker.get_snapshot()
        self._emit_event(
            "paper.session.stopped",
            {
                "total_orders": self._stats.total_orders,
                "fill_rate": round(self._stats.fill_rate, 4),
                "total_pnl": round(snapshot.total_pnl, 2),
            },
        )
        self._emit_metric_gauge("paper_backend.total_equity", snapshot.total_equity)
        logger.info(
            "paper_backend.disconnected",
            total_orders=self._stats.total_orders,
            fill_rate=round(self._stats.fill_rate, 4),
            total_pnl=round(self._tracker.total_pnl, 2),
        )

    async def execute(self, order: Any, market_price: float, costs: Any) -> FillResult:
        if not self._connected:
            return FillResult(success=False, reason="Paper backend not connected")

        symbol = getattr(order, "symbol", "")
        side = getattr(order, "side", "")
        side_str = side.value if hasattr(side, "value") else str(side)
        quantity = getattr(order, "quantity", 0)

        if quantity <= 0:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        self.update_market_price(symbol, market_price)

        risk_result = self._check_risk(symbol, side_str, quantity, market_price)
        if not risk_result.approved:
            self._record_rejection(symbol)
            self._emit_event("paper.order.rejected", {
                "symbol": symbol,
                "side": side_str,
                "quantity": quantity,
                "reason": risk_result.reason,
            })
            self._emit_metric_counter("paper_backend.orders_rejected", tags={"symbol": symbol})
            return FillResult(success=False, reason=risk_result.reason)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        if not self._check_fill_probability():
            self._record_rejection(symbol)
            self._emit_event("paper.order.rejected", {
                "symbol": symbol,
                "side": side_str,
                "quantity": quantity,
                "reason": "Simulated fill rejection (market conditions)",
            })
            self._emit_metric_counter("paper_backend.orders_rejected", tags={"symbol": symbol})
            return FillResult(success=False, reason="Simulated fill rejection (market conditions)")

        start_mono = self._clock.monotonic()

        slippage = self._compute_slippage(symbol, side_str, quantity, refreshed, costs)
        fill_price = self._apply_slippage(side_str, refreshed, slippage)

        fill_quantity = self._compute_fill_quantity(quantity)

        commission_quote = self._commission.calculate(fill_quantity, fill_price, side_str)

        latency = self._simulate_latency()
        if latency > 0:
            await asyncio.sleep(latency / 1000.0)

        self._apply_fill(symbol, side_str, fill_quantity, fill_price, commission_quote.total)

        elapsed_ms = (self._clock.monotonic() - start_mono) * 1000.0
        is_partial = fill_quantity < quantity
        slippage_bps = (
            (abs(fill_price - refreshed) / refreshed) * 10_000 if refreshed > 0 else 0.0
        )
        self._record_fill(symbol, fill_quantity, fill_price, elapsed_ms, slippage_bps, is_partial)

        fill_id = str(uuid.uuid4())
        fill_record = PaperTradeFill(
            fill_id=fill_id,
            order_id=getattr(order, "id", "unknown"),
            symbol=symbol,
            side=side_str,
            quantity=fill_quantity,
            price=fill_price,
            commission=commission_quote.total,
            timestamp=self._now_iso(),
            slippage_bps=slippage_bps,
        )
        self._fills.append(fill_record)

        event_name = "paper.order.filled"
        event_data: dict[str, Any] = {
            "fill_id": fill_id,
            "order_id": getattr(order, "id", "unknown"),
            "symbol": symbol,
            "side": side_str,
            "fill_quantity": fill_quantity,
            "fill_price": round(fill_price, 4),
            "commission": commission_quote.total,
            "slippage_bps": round(slippage_bps, 4),
            "is_partial": is_partial,
        }
        self._emit_event(event_name, event_data)
        self._emit_metric_counter(
            "paper_backend.orders_filled",
            tags={"symbol": symbol, "side": side_str},
        )
        self._emit_metric_histogram(
            "paper_backend.fill_latency_ms",
            elapsed_ms, tags={"symbol": symbol},
        )
        self._emit_metric_histogram(
            "paper_backend.slippage_bps",
            slippage_bps, tags={"symbol": symbol},
        )

        if is_partial:
            self._emit_event("paper.order.partial_fill", event_data)

        self._emit_portfolio_update()

        logger.info(
            "paper_backend.fill",
            symbol=symbol,
            side=side_str,
            requested_qty=quantity,
            fill_qty=fill_quantity,
            market_price=refreshed,
            fill_price=round(fill_price, 4),
            commission=commission_quote.total,
            latency_ms=round(elapsed_ms, 2),
            is_partial=is_partial,
        )

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=fill_quantity,
        )

    async def submit_order(  # noqa: PLR0911
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> FillResult:
        if not self._connected:
            return FillResult(success=False, reason=OrderRejectReason.NOT_CONNECTED)

        if quantity <= 0:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        market_price = self._market_prices.get(symbol, 0.0)
        if market_price <= 0:
            refreshed = await self._maybe_refresh_price(symbol, 0.0)
            if refreshed <= 0:
                return FillResult(
                    success=False,
                    reason=f"No market price available for {symbol}",
                )
            market_price = refreshed
            self.update_market_price(symbol, market_price)

        risk_result = self._check_risk(symbol, side, quantity, market_price)
        if not risk_result.approved:
            self._record_rejection(symbol)
            self._emit_event("paper.order.rejected", {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "reason": risk_result.reason,
            })
            self._emit_metric_counter("paper_backend.orders_rejected", tags={"symbol": symbol})
            return FillResult(success=False, reason=risk_result.reason)

        order_id = str(uuid.uuid4())
        now = self._now_iso()

        self._emit_event("paper.order.accepted", {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
        })

        if order_type == "market":
            return await self._execute_market_order(
                order_id, symbol, side, quantity, market_price, now,
            )
        if order_type == "limit":
            return await self._execute_limit_order(
                order_id, symbol, side, quantity, limit_price, market_price, now,
            )
        if order_type == "stop":
            return await self._execute_stop_order(
                order_id, symbol, side, quantity, stop_price, market_price, now,
            )
        if order_type == "stop_limit":
            return await self._execute_stop_limit_order(
                order_id, symbol, side, quantity, limit_price, stop_price, market_price, now,
            )

        self._open_orders[order_id] = _OpenOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            original_quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            status=PaperOrderStatus.ACCEPTED,
            created_at=now,
        )
        return FillResult(success=True, price=market_price, quantity=quantity)

    async def _execute_market_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        market_price: float,
        created_at: str,
    ) -> FillResult:
        if not self._check_fill_probability():
            self._record_rejection(symbol)
            self._emit_event("paper.order.rejected", {
                "order_id": order_id,
                "symbol": symbol,
                "reason": "Simulated fill rejection",
            })
            return FillResult(success=False, reason="Simulated fill rejection")

        refreshed = await self._maybe_refresh_price(symbol, market_price)
        slippage = self._compute_slippage(symbol, side, quantity, refreshed, None)
        fill_price = self._apply_slippage(side, refreshed, slippage)
        fill_quantity = self._compute_fill_quantity(quantity)

        commission_quote = self._commission.calculate(fill_quantity, fill_price, side)

        latency = self._simulate_latency()
        if latency > 0:
            await asyncio.sleep(latency / 1000.0)

        self._apply_fill(symbol, side, fill_quantity, fill_price, commission_quote.total)

        slippage_bps = (
            abs(fill_price - refreshed) / refreshed * 10_000
        ) if refreshed > 0 else 0.0
        is_partial = fill_quantity < quantity
        self._record_fill(symbol, fill_quantity, fill_price, 0.0, slippage_bps, is_partial)

        now = self._now_iso()
        status = PaperOrderStatus.PARTIALLY_FILLED if is_partial else PaperOrderStatus.FILLED
        self._order_history.append(_OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="market",
            status=status,
            fill_price=round(fill_price, 4),
            fill_quantity=fill_quantity,
            commission=commission_quote.total,
            created_at=created_at,
            filled_at=now,
            reject_reason="",
        ))

        self._emit_event("paper.order.filled", {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "fill_quantity": fill_quantity,
            "fill_price": round(fill_price, 4),
            "is_partial": is_partial,
        })
        self._emit_metric_counter(
            "paper_backend.orders_filled",
            tags={"symbol": symbol, "side": side},
        )
        self._emit_portfolio_update()

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=fill_quantity,
        )

    async def _execute_limit_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float | None,
        market_price: float,
        created_at: str,
    ) -> FillResult:
        if limit_price is None:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        can_fill = (
            (side == "buy" and refreshed <= limit_price)
            or (side == "sell" and refreshed >= limit_price)
        )

        if not can_fill:
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                original_quantity=quantity,
                order_type="limit",
                limit_price=limit_price,
                stop_price=None,
                status=PaperOrderStatus.ACCEPTED,
                created_at=created_at,
            )
            logger.info(
                "paper_backend.limit_resting",
                order_id=order_id,
                symbol=symbol,
                side=side,
                limit_price=limit_price,
                market_price=refreshed,
            )
            return FillResult(
                success=False,
                reason=f"Limit order resting: market {refreshed} vs limit {limit_price}",
            )

        fill_price = limit_price
        commission_quote = self._commission.calculate(quantity, fill_price, side)
        self._apply_fill(symbol, side, quantity, fill_price, commission_quote.total)
        self._record_fill(symbol, quantity, fill_price, 0.0, 0.0, False)

        now = self._now_iso()
        self._order_history.append(_OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="limit",
            status=PaperOrderStatus.FILLED,
            fill_price=round(fill_price, 4),
            fill_quantity=quantity,
            commission=commission_quote.total,
            created_at=created_at,
            filled_at=now,
            reject_reason="",
        ))

        self._emit_event("paper.order.filled", {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "fill_quantity": quantity,
            "fill_price": round(fill_price, 4),
        })
        self._emit_metric_counter(
            "paper_backend.orders_filled",
            tags={"symbol": symbol, "order_type": "limit"},
        )
        self._emit_portfolio_update()

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=quantity,
        )

    async def _execute_stop_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        stop_price: float | None,
        market_price: float,
        created_at: str,
    ) -> FillResult:
        if stop_price is None:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        triggered = (
            (side == "buy" and refreshed >= stop_price)
            or (side == "sell" and refreshed <= stop_price)
        )

        if not triggered:
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                original_quantity=quantity,
                order_type="stop",
                limit_price=None,
                stop_price=stop_price,
                status=PaperOrderStatus.ACCEPTED,
                created_at=created_at,
            )
            return FillResult(
                success=False,
                reason=f"Stop order resting: market {refreshed} vs stop {stop_price}",
            )

        slippage = self._compute_slippage(symbol, side, quantity, refreshed, None)
        fill_price = self._apply_slippage(side, refreshed, slippage)
        commission_quote = self._commission.calculate(quantity, fill_price, side)
        self._apply_fill(symbol, side, quantity, fill_price, commission_quote.total)
        slippage_bps = (
            (abs(fill_price - refreshed) / refreshed * 10_000) if refreshed > 0 else 0.0
        )
        self._record_fill(symbol, quantity, fill_price, 0.0, slippage_bps, False)

        now = self._now_iso()
        self._order_history.append(_OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="stop",
            status=PaperOrderStatus.FILLED,
            fill_price=round(fill_price, 4),
            fill_quantity=quantity,
            commission=commission_quote.total,
            created_at=created_at,
            filled_at=now,
            reject_reason="",
        ))

        self._emit_event("paper.order.filled", {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "fill_quantity": quantity,
            "fill_price": round(fill_price, 4),
        })
        self._emit_metric_counter(
            "paper_backend.orders_filled",
            tags={"symbol": symbol, "order_type": "stop"},
        )
        self._emit_portfolio_update()

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=quantity,
        )

    async def _execute_stop_limit_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float | None,
        stop_price: float | None,
        market_price: float,
        created_at: str,
    ) -> FillResult:
        if stop_price is None or limit_price is None:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        triggered = (
            (side == "buy" and refreshed >= stop_price)
            or (side == "sell" and refreshed <= stop_price)
        )

        if not triggered:
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                original_quantity=quantity,
                order_type="stop_limit",
                limit_price=limit_price,
                stop_price=stop_price,
                status=PaperOrderStatus.ACCEPTED,
                created_at=created_at,
            )
            return FillResult(
                success=False,
                reason=f"Stop-limit resting: market {refreshed} vs stop {stop_price}",
            )

        can_fill = (
            (side == "buy" and refreshed <= limit_price)
            or (side == "sell" and refreshed >= limit_price)
        )

        if can_fill:
            fill_price = limit_price
        else:
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                original_quantity=quantity,
                order_type="stop_limit",
                limit_price=limit_price,
                stop_price=stop_price,
                status=PaperOrderStatus.ACCEPTED,
                created_at=created_at,
            )
            return FillResult(
                success=False,
                reason=(
                    f"Stop triggered but limit not met: market"
                    f" {refreshed} vs limit {limit_price}"
                ),
            )

        commission_quote = self._commission.calculate(quantity, fill_price, side)
        self._apply_fill(symbol, side, quantity, fill_price, commission_quote.total)
        self._record_fill(symbol, quantity, fill_price, 0.0, 0.0, False)

        now = self._now_iso()
        self._order_history.append(_OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="stop_limit",
            status=PaperOrderStatus.FILLED,
            fill_price=round(fill_price, 4),
            fill_quantity=quantity,
            commission=commission_quote.total,
            created_at=created_at,
            filled_at=now,
            reject_reason="",
        ))

        self._emit_event("paper.order.filled", {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "fill_quantity": quantity,
            "fill_price": round(fill_price, 4),
        })
        self._emit_metric_counter(
            "paper_backend.orders_filled",
            tags={"symbol": symbol, "order_type": "stop_limit"},
        )
        self._emit_portfolio_update()

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=quantity,
        )

    async def cancel_order(self, order_id: str) -> bool:
        if order_id not in self._open_orders:
            return False
        order = self._open_orders.pop(order_id)
        now = self._now_iso()
        self._order_history.append(_OrderRecord(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            status=PaperOrderStatus.CANCELLED,
            fill_price=0.0,
            fill_quantity=0,
            commission=0.0,
            created_at=order.created_at,
            filled_at=now,
            reject_reason="",
        ))
        self._emit_event("paper.order.cancelled", {
            "order_id": order_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
        })
        self._emit_metric_counter("paper_backend.orders_cancelled", tags={"symbol": order.symbol})
        logger.info("paper_backend.order_cancelled", order_id=order_id)
        return True

    async def modify_order(
        self,
        order_id: str,
        *,
        quantity: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> bool:
        if order_id not in self._open_orders:
            return False
        order = self._open_orders[order_id]
        if quantity is not None:
            if quantity <= 0:
                return False
            order.quantity = quantity
        if limit_price is not None:
            order.limit_price = limit_price
        if stop_price is not None:
            order.stop_price = stop_price
        self._emit_event("paper.order.modified", {
            "order_id": order_id,
            "symbol": order.symbol,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "stop_price": order.stop_price,
        })
        logger.info(
            "paper_backend.order_modified",
            order_id=order_id,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
        )
        return True

    def try_fill_open_orders(  # noqa: PLR0912
        self, symbol: str,
    ) -> list[FillResult]:
        if symbol not in self._market_prices:
            return []
        market_price = self._market_prices[symbol]
        results: list[FillResult] = []
        to_fill: list[str] = []

        for order_id, order in self._open_orders.items():
            if order.symbol != symbol:
                continue
            if order.status != PaperOrderStatus.ACCEPTED:
                continue

            should_fill = False
            fill_price = market_price

            if order.order_type == "limit":
                if (
                    (order.side == "buy" and market_price <= (order.limit_price or float("inf")))
                    or (order.side == "sell" and market_price >= (order.limit_price or 0))
                ):
                    should_fill = True
                    fill_price = order.limit_price or market_price
            elif order.order_type == "stop":
                if (
                    (order.side == "buy" and market_price >= (order.stop_price or float("inf")))
                    or (order.side == "sell" and market_price <= (order.stop_price or 0))
                ):
                    should_fill = True
            elif order.order_type == "stop_limit":
                triggered = (
                    (order.side == "buy" and market_price >= (order.stop_price or float("inf")))
                    or (order.side == "sell" and market_price <= (order.stop_price or 0))
                )
                if triggered:
                    can_fill = (
                        (
                            order.side == "buy"
                            and market_price <= (order.limit_price or float("inf"))
                        )
                        or (
                            order.side == "sell"
                            and market_price >= (order.limit_price or 0)
                        )
                    )
                    if can_fill:
                        should_fill = True
                        fill_price = order.limit_price or market_price

            if should_fill:
                to_fill.append(order_id)

        for order_id in to_fill:
            order = self._open_orders.pop(order_id)
            if order is None:
                continue
            commission_quote = self._commission.calculate(
                order.quantity, fill_price, order.side,
            )
            self._apply_fill(
                order.symbol, order.side,
                order.quantity, fill_price, commission_quote.total,
            )
            slippage_bps = (
                (
                    abs(fill_price - market_price)
                    / market_price * 10_000
                ) if market_price > 0 else 0.0
            )
            self._record_fill(order.symbol, order.quantity, fill_price, 0.0, slippage_bps, False)

            now = self._now_iso()
            self._order_history.append(_OrderRecord(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type=order.order_type,
                status=PaperOrderStatus.FILLED,
                fill_price=round(fill_price, 4),
                fill_quantity=order.quantity,
                commission=commission_quote.total,
                created_at=order.created_at,
                filled_at=now,
                reject_reason="",
            ))

            result = FillResult(success=True, price=round(fill_price, 4), quantity=order.quantity)
            results.append(result)

            self._emit_event("paper.order.filled", {
                "order_id": order_id,
                "symbol": order.symbol,
                "side": order.side,
                "fill_quantity": order.quantity,
                "fill_price": round(fill_price, 4),
                "trigger": "price_update",
            })
            self._emit_metric_counter(
                "paper_backend.orders_filled",
                tags={
                    "symbol": order.symbol,
                    "trigger": "price_update",
                },
            )

        if results:
            self._emit_portfolio_update()
        return results

    async def get_positions(self) -> dict[str, PaperPosition]:
        return self._tracker.get_positions()

    async def get_portfolio(self) -> PaperPortfolioSnapshot:
        positions = self._tracker.get_positions()
        snapshot = self._tracker.get_snapshot()
        return PaperPortfolioSnapshot(
            total_equity=snapshot.total_equity,
            cash=snapshot.cash,
            positions=positions,
            unrealized_pnl=snapshot.total_unrealized_pnl,
            realized_pnl=snapshot.total_realized_pnl,
            total_pnl=snapshot.total_pnl,
            timestamp=self._now_iso(),
        )

    async def get_order_history(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        records = self._order_history[offset:offset + limit]
        return [r.as_dict() for r in records]

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "order_type": o.order_type,
                "limit_price": o.limit_price,
                "stop_price": o.stop_price,
                "status": o.status.value,
                "created_at": o.created_at,
            }
            for o in self._open_orders.values()
        ]

    async def get_fill_stats(self) -> dict[str, Any]:
        return {
            "global": self._stats.as_dict(),
            "per_symbol": {
                sym: stats.as_dict()
                for sym, stats in self._per_symbol_stats.items()
            },
            "portfolio": {
                "total_pnl": round(self._tracker.total_pnl, 2),
                "realized_pnl": round(self._tracker.total_realized_pnl, 2),
                "unrealized_pnl": round(self._tracker.total_unrealized_pnl, 2),
                "max_drawdown": round(self._tracker.max_drawdown, 4),
                "win_rate": round(self._tracker.win_rate, 4),
                "total_equity": round(self._tracker.total_equity, 2),
                "cash": round(self._tracker.cash, 2),
            },
        }

    def get_fills(self, limit: int = 100, offset: int = 0) -> list[PaperTradeFill]:
        return self._fills[offset:offset + limit]

    def update_market_price(self, symbol: str, price: float) -> None:
        self._market_prices[symbol] = price
        self._tracker.update_price(symbol, price)
        self.try_fill_open_orders(symbol)
        self._emit_metric_gauge("paper_backend.market_price", price, tags={"symbol": symbol})

    def update_market_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            self._market_prices[symbol] = price
            self._tracker.update_price(symbol, price)
        for symbol in prices:
            self.try_fill_open_orders(symbol)

    def get_state(self) -> dict[str, Any]:
        snapshot = self._tracker.get_snapshot()
        return {
            "cash": round(self._tracker.cash, 2),
            "initial_equity": self._initial_equity,
            "total_pnl": round(snapshot.total_pnl, 2),
            "total_realized_pnl": round(snapshot.total_realized_pnl, 2),
            "total_unrealized_pnl": round(snapshot.total_unrealized_pnl, 2),
            "total_equity": round(snapshot.total_equity, 2),
            "max_drawdown": round(snapshot.max_drawdown, 4),
            "win_rate": round(snapshot.win_rate, 4),
            "market_prices": dict(self._market_prices),
            "open_orders": [
                {
                    "order_id": o.order_id,
                    "symbol": o.symbol,
                    "side": o.side,
                    "quantity": o.quantity,
                    "order_type": o.order_type,
                    "limit_price": o.limit_price,
                    "stop_price": o.stop_price,
                    "status": o.status.value,
                    "created_at": o.created_at,
                }
                for o in self._open_orders.values()
            ],
            "stats": self._stats.as_dict(),
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._market_prices = state.get("market_prices", {})
        for symbol, price in self._market_prices.items():
            self._tracker.update_price(symbol, price)
        for order_data in state.get("open_orders", []):
            order_id = order_data["order_id"]
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=order_data["symbol"],
                side=order_data["side"],
                quantity=order_data["quantity"],
                original_quantity=order_data["quantity"],
                order_type=order_data["order_type"],
                limit_price=order_data.get("limit_price"),
                stop_price=order_data.get("stop_price"),
                status=PaperOrderStatus(order_data["status"]),
                created_at=order_data["created_at"],
            )
        logger.info(
            "paper_backend.state_restored",
            open_orders=len(self._open_orders),
            market_prices=len(self._market_prices),
        )

    def _emit_portfolio_update(self) -> None:
        snapshot = self._tracker.get_snapshot()
        self._emit_event("paper.portfolio.updated", {
            "total_equity": round(snapshot.total_equity, 2),
            "cash": round(snapshot.cash, 2),
            "unrealized_pnl": round(snapshot.total_unrealized_pnl, 2),
            "realized_pnl": round(snapshot.total_realized_pnl, 2),
            "total_pnl": round(snapshot.total_pnl, 2),
            "open_positions": len(snapshot.positions),
        })
        self._emit_metric_gauge("paper_backend.total_equity", snapshot.total_equity)
        self._emit_metric_gauge("paper_backend.unrealized_pnl", snapshot.total_unrealized_pnl)
        self._emit_metric_gauge("paper_backend.realized_pnl", snapshot.total_realized_pnl)
        self._emit_metric_gauge("paper_backend.open_positions", float(len(snapshot.positions)))

    def _check_risk(  # noqa: PLR0911
        self, symbol: str, side: str, quantity: int, price: float,
    ) -> _RiskCheck:
        risk = self._risk_config
        trade_value = quantity * price

        if trade_value > risk.max_single_order_value:
            return _RiskCheck(
                approved=False,
                reason=(
                    f"Order value ${trade_value:,.0f} exceeds"
                    f" max ${risk.max_single_order_value:,.0f}"
                ),
                reject_reason=OrderRejectReason.RISK_LIMIT_EXCEEDED,
            )

        current_qty = self._tracker.get_position_quantity(symbol)
        new_qty = current_qty + quantity if side == "buy" else current_qty - quantity

        if abs(new_qty) > risk.max_position_size:
            return _RiskCheck(
                approved=False,
                reason=f"Position size {abs(new_qty)} would exceed max {risk.max_position_size}",
                reject_reason=OrderRejectReason.MAX_POSITION_EXCEEDED,
            )

        now = self._clock.monotonic()
        one_interval_ago = now - 60.0
        recent = sum(1 for t in self._order_timestamps if t > one_interval_ago)
        if recent >= risk.max_orders_per_minute:
            return _RiskCheck(
                approved=False,
                reason=f"Order rate {recent}/min exceeds max {risk.max_orders_per_minute}",
                reject_reason=OrderRejectReason.ORDER_RATE_EXCEEDED,
            )

        if self._tracker.total_pnl < -(self._initial_equity * risk.max_daily_loss_pct):
            return _RiskCheck(
                approved=False,
                reason=f"Daily loss limit exceeded: P&L {self._tracker.total_pnl:,.2f}",
                reject_reason=OrderRejectReason.DAILY_LOSS_EXCEEDED,
            )

        if symbol in risk.banned_symbols:
            return _RiskCheck(
                approved=False,
                reason=f"Symbol {symbol} is banned",
                reject_reason=OrderRejectReason.SYMBOL_BANNED,
            )

        if risk.allowed_symbols is not None and symbol not in risk.allowed_symbols:
            return _RiskCheck(
                approved=False,
                reason=f"Symbol {symbol} is not in allowed list",
                reject_reason=OrderRejectReason.SYMBOL_BANNED,
            )

        open_positions = len(self._tracker.get_positions())
        if (
            side == "buy"
            and symbol not in self._tracker.get_positions()
            and open_positions >= risk.max_open_positions
        ):
            return _RiskCheck(
                approved=False,
                reason=f"Max open positions reached: {risk.max_open_positions}",
                reject_reason=OrderRejectReason.RISK_LIMIT_EXCEEDED,
            )

        self._order_timestamps.append(now)
        return _RiskCheck(approved=True)

    def _check_fill_probability(self) -> bool:
        return self._rng.random() <= self._config.fill_probability

    def _compute_slippage(
        self, symbol: str, side: str, quantity: int, price: float, costs: Any
    ) -> float:
        cost_slippage = 0.0
        if costs is not None and hasattr(costs, "slippage"):
            slippage_obj = costs.slippage
            cost_slippage = (
                slippage_obj.amount
                if hasattr(slippage_obj, "amount")
                else float(slippage_obj)
            )

        if cost_slippage > 0:
            return cost_slippage / quantity if quantity > 0 else 0.0

        ctx = SlippageContext(
            symbol=symbol,
            side=side,
            quantity=quantity,
            market_price=price,
        )
        return self._slippage.compute(ctx)

    def _apply_slippage(self, side: str, price: float, slippage: float) -> float:
        if side == "buy":
            return price + slippage
        return price - slippage

    def _compute_fill_quantity(self, requested: int) -> int:
        if not self._config.partial_fill_enabled or requested <= 1:
            return requested
        ratio = self._rng.uniform(
            self._config.partial_fill_min_ratio,
            _PARTIAL_FILL_MAX_RATIO,
        )
        return max(1, int(requested * ratio))

    def _simulate_latency(self) -> float:
        base = self._config.latency_ms
        jitter = self._config.latency_jitter_ms
        latency = max(0.0, base + self._rng.gauss(0, jitter / 2))
        return min(latency, base + jitter * 3)

    def _apply_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        commission: float,
    ) -> None:
        if side == "buy":
            self._tracker.open_or_update(symbol, quantity, price, commission)
        elif side == "sell":
            current = self._tracker.get_position_quantity(symbol)
            if current > 0:
                close_qty = min(quantity, current)
                if close_qty > 0:
                    self._tracker.open_or_update(symbol, -close_qty, price, commission)
                remaining = quantity - close_qty
                if remaining > 0:
                    self._tracker.open_or_update(symbol, -remaining, price, commission)
            else:
                self._tracker.open_or_update(symbol, -quantity, price, commission)

    async def _maybe_refresh_price(self, symbol: str, fallback: float) -> float:
        if not self._config.refresh_price_from_provider or self._data_provider is None:
            return fallback
        try:
            latest = await self._data_provider.get_latest_price(symbol)
            if latest is not None and latest > 0:
                self.update_market_price(symbol, latest)
                return latest
        except Exception:
            logger.debug("paper_backend.price_refresh_failed", symbol=symbol)
        return fallback

    def _record_rejection(self, symbol: str) -> None:
        self._stats.total_orders += 1
        self._stats.rejected_orders += 1
        self._per_symbol_stats[symbol].total_orders += 1
        self._per_symbol_stats[symbol].rejected_orders += 1

    def _record_fill(
        self,
        symbol: str,
        quantity: int,
        price: float,
        elapsed_ms: float,
        slippage_bps: float,
        is_partial: bool,
    ) -> None:
        notional = quantity * price

        self._stats.total_orders += 1
        self._stats.filled_orders += 1
        self._stats.total_latency_ms += elapsed_ms
        self._stats.total_notional += notional
        self._stats.total_slippage_bps += slippage_bps
        if is_partial:
            self._stats.partial_fills += 1

        sym_stats = self._per_symbol_stats[symbol]
        sym_stats.total_orders += 1
        sym_stats.filled_orders += 1
        sym_stats.total_latency_ms += elapsed_ms
        sym_stats.total_notional += notional
        sym_stats.total_slippage_bps += slippage_bps
        if is_partial:
            sym_stats.partial_fills += 1

    def reset_stats(self) -> None:
        self._stats = _FillStats()
        self._per_symbol_stats.clear()
