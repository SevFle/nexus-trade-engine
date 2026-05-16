"""
Paper trading execution backend.

Uses live market data but simulates order execution with configurable
fill probability, slippage models, partial fills, and latency simulation.
Bridges the gap between backtest and live.

Distinct from BacktestBackend:
  - Pluggable slippage models (not fixed bps)
  - Configurable fill probability per order type
  - Simulated execution latency
  - Partial fill support with configurable ratios
  - Fill statistics tracking
  - Optional live price refresh via data provider
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.execution.base import ExecutionBackend, FillResult
from engine.core.execution.slippage import (
    SlippageContext,
    SlippageModel,
    SlippageModelType,
    create_slippage_model,
)

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order

logger = structlog.get_logger()

_PARTIAL_FILL_MIN_RATIO = 0.5
_PARTIAL_FILL_MAX_RATIO = 1.0


@dataclass
class PaperFillStats:
    total_orders: int = 0
    filled_orders: int = 0
    partial_fills: int = 0
    rejected_orders: int = 0
    total_latency_ms: float = 0.0
    total_notional: float = 0.0
    total_slippage_bps: float = 0.0

    @property
    def fill_rate(self) -> float:
        return self.filled_orders / self.total_orders if self.total_orders > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.filled_orders if self.filled_orders > 0 else 0.0

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
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "total_notional": round(self.total_notional, 2),
            "avg_slippage_bps": round(self.avg_slippage_bps, 4),
        }


@dataclass
class PaperTradeConfig:
    fill_probability: float = 0.95
    partial_fill_enabled: bool = True
    partial_fill_min_ratio: float = 0.5
    latency_ms: float = 50.0
    latency_jitter_ms: float = 20.0
    random_seed: int | None = None
    slippage_model: SlippageModel | None = None
    slippage_model_type: SlippageModelType = SlippageModelType.FIXED_BPS
    slippage_model_kwargs: dict[str, Any] = field(default_factory=dict)
    refresh_price_from_provider: bool = False


class PaperBackend(ExecutionBackend):
    """
    Paper trading: live data, simulated execution.

    More realistic than backtest because it uses real-time prices,
    but no real money is at risk. Features:

    - Configurable fill probability (default 95%)
    - Pluggable slippage models (fixed, percentage, sqrt, volume-weighted, random)
    - Partial fill simulation
    - Latency simulation with jitter
    - Fill statistics tracking
    - Optional live price refresh via data provider
    """

    def __init__(
        self,
        config: PaperTradeConfig | None = None,
        *,
        data_provider: Any = None,
    ):
        self._config = config or PaperTradeConfig()
        self._rng = random.Random(self._config.random_seed)  # noqa: S311
        self._connected = False
        self._data_provider = data_provider
        self._stats = PaperFillStats()
        self._per_symbol_stats: dict[str, PaperFillStats] = defaultdict(PaperFillStats)

        self._slippage = self._config.slippage_model or create_slippage_model(
            self._config.slippage_model_type,
            **self._config.slippage_model_kwargs,
        )

    @property
    def config(self) -> PaperTradeConfig:
        return self._config

    @property
    def stats(self) -> PaperFillStats:
        return self._stats

    @property
    def connected(self) -> bool:
        return self._connected

    def get_symbol_stats(self, symbol: str) -> PaperFillStats:
        return self._per_symbol_stats.get(symbol, PaperFillStats())

    async def connect(self) -> None:
        self._connected = True
        logger.info(
            "paper.backend.connected",
            fill_probability=self._config.fill_probability,
            slippage_model=type(self._slippage).__name__,
            latency_ms=self._config.latency_ms,
        )

    async def disconnect(self) -> None:
        self._connected = False
        logger.info(
            "paper.backend.disconnected",
            total_orders=self._stats.total_orders,
            fill_rate=round(self._stats.fill_rate, 4),
        )

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        if not self._connected:
            return FillResult(success=False, reason="Paper backend not connected")

        if order.quantity <= 0:
            return FillResult(success=False, reason="Order quantity must be positive")

        start = time.monotonic()

        refreshed_price = await self._maybe_refresh_price(order.symbol, market_price)
        effective_price = refreshed_price

        if not self._check_fill_probability():
            self._record_rejection(order.symbol)
            return FillResult(success=False, reason="Simulated fill rejection (market conditions)")

        slippage_per_share = self._compute_slippage(order, effective_price, costs)
        fill_price = self._apply_slippage_to_price(order, effective_price, slippage_per_share)

        fill_quantity = self._compute_fill_quantity(order)
        is_partial = fill_quantity < order.quantity

        latency = self._simulate_latency()
        await asyncio.sleep(latency / 1000.0)

        elapsed_ms = (time.monotonic() - start) * 1000.0
        slippage_bps = (
            (abs(fill_price - effective_price) / effective_price) * 10_000
            if effective_price > 0
            else 0.0
        )

        self._record_fill(
            order.symbol, fill_quantity, fill_price, elapsed_ms, slippage_bps, is_partial
        )

        logger.info(
            "paper.fill",
            symbol=order.symbol,
            side=order.side.value,
            requested_qty=order.quantity,
            fill_qty=fill_quantity,
            market_price=effective_price,
            fill_price=round(fill_price, 4),
            slippage_per_share=round(slippage_per_share, 4),
            latency_ms=round(elapsed_ms, 2),
            is_partial=is_partial,
        )

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=fill_quantity,
        )

    async def _maybe_refresh_price(self, symbol: str, fallback_price: float) -> float:
        if not self._config.refresh_price_from_provider or self._data_provider is None:
            return fallback_price
        try:
            latest = await self._data_provider.get_latest_price(symbol)
            if latest is not None and latest > 0:
                return latest
        except Exception:
            logger.debug("paper.price_refresh_failed", symbol=symbol)
        return fallback_price

    def _check_fill_probability(self) -> bool:
        return self._rng.random() <= self._config.fill_probability

    def _compute_slippage(
        self, order: Order, market_price: float, costs: CostBreakdown
    ) -> float:
        ctx = SlippageContext(
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            market_price=market_price,
            costs=costs,
        )
        return self._slippage.compute(ctx)

    def _apply_slippage_to_price(
        self, order: Order, price: float, slippage_per_share: float
    ) -> float:
        if order.side.value == "buy":
            return price + slippage_per_share
        return price - slippage_per_share

    def _compute_fill_quantity(self, order: Order) -> int:
        if not self._config.partial_fill_enabled:
            return order.quantity

        if order.quantity <= 1:
            return order.quantity

        min_ratio = self._config.partial_fill_min_ratio
        fill_ratio = self._rng.uniform(min_ratio, _PARTIAL_FILL_MAX_RATIO)
        return max(1, int(order.quantity * fill_ratio))

    def _simulate_latency(self) -> float:
        base = self._config.latency_ms
        jitter = self._config.latency_jitter_ms
        latency = max(0.0, base + self._rng.gauss(0, jitter / 2))
        return min(latency, base + jitter * 3)

    def _record_rejection(self, symbol: str) -> None:
        self._stats.total_orders += 1
        self._stats.rejected_orders += 1
        sym_stats = self._per_symbol_stats[symbol]
        sym_stats.total_orders += 1
        sym_stats.rejected_orders += 1

    def _record_fill(
        self,
        symbol: str,
        quantity: int,
        fill_price: float,
        elapsed_ms: float,
        slippage_bps: float,
        is_partial: bool,
    ) -> None:
        notional = quantity * fill_price

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
        self._stats = PaperFillStats()
        self._per_symbol_stats.clear()
