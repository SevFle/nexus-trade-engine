"""
Execution backends — the swappable layer that makes strategies
run identically in backtest, paper, and live modes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order


@dataclass
class FillResult:
    """Result of an order execution attempt."""

    success: bool
    price: float = 0.0
    quantity: int = 0
    reason: str = ""
    costs: object | None = None


class ExecutionBackend(ABC):
    """
    Abstract execution backend.

    The OrderManager calls execute() without knowing which backend is active.
    Strategies never interact with backends directly.
    """

    @abstractmethod
    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        """
        Execute an order and return the fill result.

        Args:
            order: The validated, risk-checked order.
            market_price: Current market price at time of execution.
            costs: Pre-calculated cost breakdown (slippage modeled here).

        Returns:
            FillResult with actual fill price and quantity.
        """
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connection (broker API, data source, etc.)."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean shutdown."""
        ...
