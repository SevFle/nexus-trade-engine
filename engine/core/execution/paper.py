"""
Paper trading execution backend.

Uses live market data but simulates order execution.
Bridges the gap between backtest and live.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.execution.base import ExecutionBackend, FillResult
from engine.observability.metrics import MetricsBackend, get_metrics

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order

logger = structlog.get_logger()


class SlippageModel(StrEnum):
    FIXED = "fixed"
    PERCENTAGE = "percentage"
    RANDOM = "random"


@dataclass
class PaperFillStats:
    total_fills: int = 0
    successful_fills: int = 0
    failed_fills: int = 0
    partial_fills: int = 0
    total_slippage_bps: float = 0.0
    total_fill_quantity: int = 0
    total_fill_value: float = 0.0

    @property
    def avg_slippage_bps(self) -> float:
        if self.successful_fills == 0:
            return 0.0
        return self.total_slippage_bps / self.successful_fills

    @property
    def fill_rate(self) -> float:
        if self.total_fills == 0:
            return 0.0
        return self.successful_fills / self.total_fills

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_fills": self.total_fills,
            "successful_fills": self.successful_fills,
            "failed_fills": self.failed_fills,
            "partial_fills": self.partial_fills,
            "avg_slippage_bps": round(self.avg_slippage_bps, 4),
            "fill_rate": round(self.fill_rate, 4),
            "total_fill_quantity": self.total_fill_quantity,
            "total_fill_value": round(self.total_fill_value, 2),
        }


PriceProvider = Callable[[str], float | None]


class PaperExecutionBackend(ExecutionBackend):
    """
    Production-grade paper trading backend.

    Uses live market data via a price provider but simulates order execution
    with configurable fill probability, slippage models, and partial fills.
    Integrates with the metrics subsystem for observability.
    """

    def __init__(
        self,
        *,
        fill_probability: float = 0.95,
        slippage_model: SlippageModel = SlippageModel.RANDOM,
        slippage_bps: float = 5.0,
        slippage_fixed_amount: float = 0.01,
        slippage_jitter_range: float = 0.3,
        partial_fill_enabled: bool = True,
        partial_fill_min_ratio: float = 0.8,
        partial_fill_volume_threshold: int = 500,
        latency_ms_mean: float = 50.0,
        latency_ms_std: float = 20.0,
        random_seed: int | None = None,
        price_provider: PriceProvider | None = None,
        metrics: MetricsBackend | None = None,
    ) -> None:
        if not 0.0 <= fill_probability <= 1.0:
            raise ValueError("fill_probability must be between 0.0 and 1.0")
        if slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if not 0.0 <= partial_fill_min_ratio <= 1.0:
            raise ValueError("partial_fill_min_ratio must be between 0.0 and 1.0")

        self.fill_probability = fill_probability
        self.slippage_model = slippage_model
        self.slippage_bps = slippage_bps
        self.slippage_fixed_amount = slippage_fixed_amount
        self.slippage_jitter_range = slippage_jitter_range
        self.partial_fill_enabled = partial_fill_enabled
        self.partial_fill_min_ratio = partial_fill_min_ratio
        self.partial_fill_volume_threshold = partial_fill_volume_threshold
        self.latency_ms_mean = latency_ms_mean
        self.latency_ms_std = latency_ms_std
        self._price_provider = price_provider
        self._metrics = metrics

        self._connected = False
        self._rng = random.Random(random_seed)
        self._stats = PaperFillStats()
        self._connected_at: float | None = None

    @property
    def metrics(self) -> MetricsBackend:
        return self._metrics if self._metrics is not None else get_metrics()

    @property
    def stats(self) -> PaperFillStats:
        return self._stats

    def set_price_provider(self, provider: PriceProvider) -> None:
        self._price_provider = provider

    async def connect(self) -> None:
        self._connected = True
        self._connected_at = time.monotonic()
        self.metrics.counter("paper_backend.connect")
        logger.info("paper.backend.connected")

    async def disconnect(self) -> None:
        if self._connected_at is not None:
            duration_s = time.monotonic() - self._connected_at
            self.metrics.histogram("paper_backend.session_duration_seconds", duration_s)
        self._connected = False
        self._connected_at = None
        self.metrics.counter("paper_backend.disconnect")
        logger.info(
            "paper.backend.disconnected",
            stats=self._stats.as_dict(),
        )

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        self._stats.total_fills += 1

        if not self._connected:
            self._stats.failed_fills += 1
            self.metrics.counter("paper_backend.execute", tags={"outcome": "not_connected"})
            return FillResult(success=False, reason="Paper backend not connected")

        effective_price = self._resolve_price(order.symbol, market_price)
        if effective_price is None or effective_price <= 0:
            self._stats.failed_fills += 1
            self.metrics.counter("paper_backend.execute", tags={"outcome": "no_price"})
            return FillResult(success=False, reason=f"No valid price for {order.symbol}")

        if not self._check_fill_probability():
            self._stats.failed_fills += 1
            self.metrics.counter("paper_backend.execute", tags={"outcome": "fill_rejected"})
            return FillResult(success=False, reason="Simulated fill failure (market conditions)")

        slippage = self._calculate_slippage(effective_price, order.quantity, costs)

        if order.side.value == "buy":
            fill_price = effective_price + slippage
        else:
            fill_price = effective_price - slippage

        fill_price = round(fill_price, 4)

        fill_quantity = self._calculate_fill_quantity(order.quantity)

        slippage_bps = abs(slippage / effective_price) * 10_000 if effective_price > 0 else 0
        self._stats.successful_fills += 1
        self._stats.total_slippage_bps += slippage_bps
        self._stats.total_fill_quantity += fill_quantity
        self._stats.total_fill_value += fill_price * fill_quantity
        if fill_quantity < order.quantity:
            self._stats.partial_fills += 1

        self.metrics.counter("paper_backend.execute", tags={"outcome": "filled"})
        self.metrics.histogram(
            "paper_backend.slippage_bps",
            slippage_bps,
            tags={"side": order.side.value},
        )

        logger.info(
            "paper.fill",
            symbol=order.symbol,
            side=order.side.value,
            qty=fill_quantity,
            requested_qty=order.quantity,
            price=fill_price,
            slippage_bps=round(slippage_bps, 2),
            partial=fill_quantity < order.quantity,
        )

        return FillResult(
            success=True,
            price=fill_price,
            quantity=fill_quantity,
        )

    def _resolve_price(self, symbol: str, market_price: float) -> float | None:
        if self._price_provider is not None:
            provider_price = self._price_provider(symbol)
            if provider_price is not None and provider_price > 0:
                return provider_price
        if market_price > 0:
            return market_price
        return None

    def _check_fill_probability(self) -> bool:
        return self._rng.random() <= self.fill_probability

    def _calculate_slippage(self, price: float, quantity: int, costs: CostBreakdown) -> float:
        if self.slippage_model == SlippageModel.FIXED:
            base_slippage = self.slippage_fixed_amount
        elif self.slippage_model == SlippageModel.PERCENTAGE:
            base_slippage = price * (self.slippage_bps / 10_000)
        else:
            cost_slippage = costs.slippage.amount / quantity if quantity > 0 else 0
            model_slippage = price * (self.slippage_bps / 10_000)
            base_slippage = (cost_slippage + model_slippage) / 2
            jitter = base_slippage * self._rng.uniform(
                -self.slippage_jitter_range,
                self.slippage_jitter_range + 0.2,
            )
            base_slippage += jitter

        return abs(base_slippage)

    def _calculate_fill_quantity(self, requested_quantity: int) -> int:
        if not self.partial_fill_enabled:
            return requested_quantity
        if requested_quantity <= self.partial_fill_volume_threshold:
            return requested_quantity

        fill_ratio = self._rng.uniform(self.partial_fill_min_ratio, 1.0)
        return max(1, int(requested_quantity * fill_ratio))


class PaperBackend(PaperExecutionBackend):
    """Backward-compatible alias for PaperExecutionBackend.

    Retains the original PaperBackend behavior: all fills succeed
    (fill_probability=1.0), no partial fills.
    """

    def __init__(self) -> None:
        super().__init__(
            fill_probability=1.0,
            partial_fill_enabled=False,
        )
