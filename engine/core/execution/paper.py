"""
Paper trading execution backend.

Uses live market data but simulates order execution.
Bridges the gap between backtest and live.
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


class PaperBackend(ExecutionBackend):
    """
    Paper trading: live data, simulated execution.

    More realistic than backtest because it uses real-time prices,
    but no real money is at risk. Adds random latency simulation.
    """

    def __init__(self):
        self._connected = False
        self._rng = random.Random()  # noqa: S311

    async def connect(self) -> None:
        self._connected = True
        logger.info("paper.backend.connected")

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("paper.backend.disconnected")

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        if not self._connected:
            return FillResult(success=False, reason="Paper backend not connected")

        # Simulate realistic slippage (slightly random around estimated)
        slippage_per_share = costs.slippage.amount / order.quantity if order.quantity > 0 else 0
        slippage_jitter = slippage_per_share * self._rng.uniform(-0.2, 0.5)
        actual_slippage = slippage_per_share + slippage_jitter

        if order.side.value == "buy":
            fill_price = market_price + actual_slippage
        else:
            fill_price = market_price - actual_slippage

        logger.info(
            "paper.fill",
            symbol=order.symbol,
            side=order.side,
            qty=order.quantity,
            price=round(fill_price, 4),
        )

        return FillResult(
            success=True,
            price=round(fill_price, 4),
            quantity=order.quantity,
        )
