"""
Backtest execution backend.

Simulates fills using historical data with realistic cost modeling.
No network calls — everything runs from local data.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import structlog
from engine.core.execution.base import ExecutionBackend, FillResult

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order

logger = structlog.get_logger()


class BacktestBackend(ExecutionBackend):
    """
    Historical simulation with realistic fill modeling.

    Fills at market_price + slippage. Supports partial fills based on
    volume constraints. Deterministic when seed is set.
    """

    def __init__(
        self,
        fill_probability: float = 0.98,
        partial_fill_enabled: bool = True,
        random_seed: int | None = None,
    ):
        self.fill_probability = fill_probability
        self.partial_fill_enabled = partial_fill_enabled
        self._rng = random.Random(random_seed)

    async def connect(self) -> None:
        logger.info("backtest.backend.ready")

    async def disconnect(self) -> None:
        pass

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        # Simulate fill probability
        if self._rng.random() > self.fill_probability:
            return FillResult(success=False, reason="Simulated fill failure (market conditions)")

        # Apply slippage to get realistic fill price
        slippage_per_share = costs.slippage.amount / order.quantity if order.quantity > 0 else 0

        if order.side.value == "buy":
            fill_price = market_price + slippage_per_share  # Pay more
        else:
            fill_price = market_price - slippage_per_share  # Receive less

        # Simulate partial fills
        fill_quantity = order.quantity
        if self.partial_fill_enabled and order.quantity > 1000:
            fill_ratio = self._rng.uniform(0.85, 1.0)
            fill_quantity = max(1, int(order.quantity * fill_ratio))

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=fill_quantity,
            costs=costs,
        )
