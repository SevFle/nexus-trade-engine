from __future__ import annotations

from dataclasses import dataclass

from engine.orders.cost_model import CostModel


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: float | None = None


class OrderManager:
    """Stub — processes orders during backtest or live execution."""

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()

    async def submit(self, order: OrderRequest) -> dict:
        raise NotImplementedError
