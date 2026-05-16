"""
PaperTradeBroker — full paper trade execution engine.

Combines fill simulation, position tracking, commission calculation,
and pre-trade risk checks into a single broker implementing
IPaperTradeBroker and ExecutionBackend. Drops in as the swappable
execution layer in the three-mode architecture.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from engine.core.execution.base import ExecutionBackend, FillResult
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
)
from engine.core.execution.position_tracker import PaperPositionTracker
from engine.core.execution.slippage import (
    SlippageContext,
    create_slippage_model,
)

logger = structlog.get_logger()

_PARTIAL_FILL_MIN_RATIO = 0.5
_PARTIAL_FILL_MAX_RATIO = 1.0


@dataclass
class _OpenOrder:
    order_id: str
    symbol: str
    side: str
    quantity: int
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


class PaperTradeBroker(ExecutionBackend):
    """
    Full-featured paper trade broker.

    Implements ExecutionBackend so it drops into OrderManager, and
    also provides IPaperTradeBroker methods for direct use in the
    paper trade runner.

    Features:
    - Pluggable slippage models (fixed, percentage, sqrt, volume-weighted, random)
    - Pluggable commission models (per-share, flat, percentage, tiered, zero)
    - Position tracking with realized/unrealized P&L
    - Pre-trade risk checks (position size, rate limit, daily loss, symbol filter)
    - Partial fill support
    - Latency simulation with jitter
    - Fill statistics tracking
    - Order lifecycle management (submit, cancel, get open/history)
    """

    def __init__(
        self,
        config: PaperTradeBrokerConfig | None = None,
        *,
        initial_cash: float = 100_000.0,
        data_provider: Any = None,
        commission_calculator: ICommissionCalculator | None = None,
    ) -> None:
        self._config = config or PaperTradeBrokerConfig()
        self._data_provider = data_provider
        self._rng = random.Random(self._config.random_seed)  # noqa: S311
        self._connected = False

        self._slippage = create_slippage_model(
            self._config.slippage_model_type,
            **self._config.slippage_model_kwargs,
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
        self._risk_config = self._config.risk_config

        self._open_orders: dict[str, _OpenOrder] = {}
        self._order_history: list[_OrderRecord] = []
        self._market_prices: dict[str, float] = {}

        self._order_timestamps: list[float] = []
        self._session_start: float | None = None
        self._initial_equity = initial_cash

        self._stats = _FillStats()
        self._per_symbol_stats: dict[str, _FillStats] = defaultdict(_FillStats)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def config(self) -> PaperTradeBrokerConfig:
        return self._config

    @property
    def position_tracker(self) -> PaperPositionTracker:
        return self._tracker

    async def connect(self) -> None:
        self._connected = True
        self._session_start = time.monotonic()
        logger.info(
            "paper_broker.connected",
            fill_probability=self._config.fill_probability,
            slippage_model=type(self._slippage).__name__,
            commission_model=type(self._commission).__name__,
            latency_ms=self._config.latency_ms,
        )

    async def disconnect(self) -> None:
        self._connected = False
        logger.info(
            "paper_broker.disconnected",
            total_orders=self._stats.total_orders,
            fill_rate=round(self._stats.fill_rate, 4),
            total_pnl=round(self._tracker.total_pnl, 2),
        )

    async def execute(self, order: Any, market_price: float, costs: Any) -> FillResult:
        if not self._connected:
            return FillResult(success=False, reason="Paper broker not connected")

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
            return FillResult(success=False, reason=risk_result.reason)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        if not self._check_fill_probability():
            self._record_rejection(symbol)
            return FillResult(success=False, reason="Simulated fill rejection (market conditions)")

        slippage = self._compute_slippage(symbol, side_str, quantity, refreshed, costs)
        fill_price = self._apply_slippage(side_str, refreshed, slippage)

        fill_quantity = self._compute_fill_quantity(quantity)

        commission_quote = self._commission.calculate(fill_quantity, fill_price, side_str)

        latency = self._simulate_latency()
        await asyncio.sleep(latency / 1000.0)

        self._apply_fill(symbol, side_str, fill_quantity, fill_price, commission_quote.total)

        is_partial = fill_quantity < quantity
        elapsed = 0.0
        slippage_bps = (
            (abs(fill_price - refreshed) / refreshed) * 10_000 if refreshed > 0 else 0.0
        )
        self._record_fill(symbol, fill_quantity, fill_price, elapsed, slippage_bps, is_partial)

        logger.info(
            "paper_broker.fill",
            symbol=symbol,
            side=side_str,
            requested_qty=quantity,
            fill_qty=fill_quantity,
            market_price=refreshed,
            fill_price=round(fill_price, 4),
            commission=commission_quote.total,
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
            return FillResult(
                success=False,
                reason=OrderRejectReason.NOT_CONNECTED,
            )

        if quantity <= 0:
            return FillResult(
                success=False,
                reason=OrderRejectReason.INVALID_ORDER,
            )

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
            return FillResult(success=False, reason=risk_result.reason)

        if order_type == "market":
            return await self._execute_market_order(symbol, side, quantity, market_price)
        if order_type == "limit":
            return await self._execute_limit_order(
                symbol, side, quantity, limit_price, market_price,
            )
        if order_type == "stop":
            return await self._execute_stop_order(symbol, side, quantity, stop_price, market_price)
        order_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self._open_orders[order_id] = _OpenOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            status=PaperOrderStatus.ACCEPTED,
            created_at=now,
        )
        return FillResult(success=True, price=market_price, quantity=quantity)

    async def _execute_market_order(
        self, symbol: str, side: str, quantity: int, market_price: float
    ) -> FillResult:
        if not self._check_fill_probability():
            self._record_rejection(symbol)
            return FillResult(success=False, reason="Simulated fill rejection")

        refreshed = await self._maybe_refresh_price(symbol, market_price)
        slippage = self._compute_slippage(symbol, side, quantity, refreshed, None)
        fill_price = self._apply_slippage(side, refreshed, slippage)
        fill_quantity = self._compute_fill_quantity(quantity)

        commission_quote = self._commission.calculate(fill_quantity, fill_price, side)

        latency = self._simulate_latency()
        await asyncio.sleep(latency / 1000.0)

        self._apply_fill(symbol, side, fill_quantity, fill_price, commission_quote.total)

        slippage_bps = (
            abs(fill_price - refreshed) / refreshed * 10_000
        ) if refreshed > 0 else 0.0
        self._record_fill(
            symbol, fill_quantity, fill_price, 0.0,
            slippage_bps, fill_quantity < quantity,
        )

        return FillResult(success=True, price=round(fill_price, 4), quantity=fill_quantity)

    async def _execute_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float | None,
        market_price: float,
    ) -> FillResult:
        if limit_price is None:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        can_fill = (
            (side == "buy" and refreshed <= limit_price)
            or (side == "sell" and refreshed >= limit_price)
        )

        if not can_fill:
            order_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type="limit",
                limit_price=limit_price,
                stop_price=None,
                status=PaperOrderStatus.ACCEPTED,
                created_at=now,
            )
            logger.info(
                "paper_broker.limit_resting",
                order_id=order_id,
                symbol=symbol,
                side=side,
                limit_price=limit_price,
                market_price=refreshed,
            )
            return FillResult(
                success=False,
                reason=(
                    f"Limit order resting: market {refreshed}"
                    f" vs limit {limit_price}"
                ),
            )

        fill_price = limit_price
        commission_quote = self._commission.calculate(quantity, fill_price, side)
        self._apply_fill(symbol, side, quantity, fill_price, commission_quote.total)
        self._record_fill(symbol, quantity, fill_price, 0.0, 0.0, False)

        return FillResult(success=True, price=round(fill_price, 4), quantity=quantity)

    async def _execute_stop_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        stop_price: float | None,
        market_price: float,
    ) -> FillResult:
        if stop_price is None:
            return FillResult(success=False, reason=OrderRejectReason.INVALID_ORDER)

        refreshed = await self._maybe_refresh_price(symbol, market_price)

        triggered = (
            (side == "buy" and refreshed >= stop_price)
            or (side == "sell" and refreshed <= stop_price)
        )

        if not triggered:
            order_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type="stop",
                limit_price=None,
                stop_price=stop_price,
                status=PaperOrderStatus.ACCEPTED,
                created_at=now,
            )
            return FillResult(
                success=False,
                reason=(
                    f"Stop order resting: market {refreshed}"
                    f" vs stop {stop_price}"
                ),
            )

        slippage = self._compute_slippage(symbol, side, quantity, refreshed, None)
        fill_price = self._apply_slippage(side, refreshed, slippage)
        commission_quote = self._commission.calculate(quantity, fill_price, side)
        self._apply_fill(symbol, side, quantity, fill_price, commission_quote.total)
        self._record_fill(
            symbol, quantity, fill_price, 0.0,
            (abs(fill_price - refreshed) / refreshed * 10_000)
            if refreshed > 0 else 0.0,
            False,
        )

        return FillResult(success=True, price=round(fill_price, 4), quantity=quantity)

    async def cancel_order(self, order_id: str) -> bool:
        if order_id not in self._open_orders:
            return False
        order = self._open_orders.pop(order_id)
        now = datetime.now(UTC).isoformat()
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
        logger.info("paper_broker.order_cancelled", order_id=order_id)
        return True

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
            timestamp=datetime.now(UTC).isoformat(),
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

    def update_market_price(self, symbol: str, price: float) -> None:
        self._market_prices[symbol] = price
        self._tracker.update_price(symbol, price)

    def _check_risk(self, symbol: str, side: str, quantity: int, price: float) -> _RiskCheck:  # noqa: PLR0911
        risk = self._risk_config
        if risk is None:
            return _RiskCheck(approved=True)

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

        now = time.monotonic()
        one_minute_ago = now - 60.0
        recent = sum(1 for t in self._order_timestamps if t > one_minute_ago)
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
            logger.debug("paper_broker.price_refresh_failed", symbol=symbol)
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
        _elapsed_ms: float,
        slippage_bps: float,
        is_partial: bool,
    ) -> None:
        notional = quantity * price

        self._stats.total_orders += 1
        self._stats.filled_orders += 1
        self._stats.total_notional += notional
        self._stats.total_slippage_bps += slippage_bps
        if is_partial:
            self._stats.partial_fills += 1

        sym_stats = self._per_symbol_stats[symbol]
        sym_stats.total_orders += 1
        sym_stats.filled_orders += 1
        sym_stats.total_notional += notional
        sym_stats.total_slippage_bps += slippage_bps
        if is_partial:
            sym_stats.partial_fills += 1


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

    @property
    def fill_rate(self) -> float:
        return self.filled_orders / self.total_orders if self.total_orders > 0 else 0.0

    @property
    def avg_slippage_bps(self) -> float:
        return self.total_slippage_bps / self.filled_orders if self.filled_orders > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_orders": self.total_orders,
            "filled_orders": self.filled_orders,
            "partial_fills": self.partial_fills,
            "rejected_orders": self.rejected_orders,
            "fill_rate": round(self.fill_rate, 4),
            "total_notional": round(self.total_notional, 2),
            "avg_slippage_bps": round(self.avg_slippage_bps, 4),
        }
