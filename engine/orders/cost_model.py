from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    """Stub — models transaction costs (commission, slippage, spread)."""

    commission_rate: float = 0.001
    slippage_bps: float = 1.0

    def total_cost(self, price: float, quantity: float) -> float:
        commission = price * quantity * self.commission_rate
        slippage = price * quantity * (self.slippage_bps / 10_000)
        return commission + slippage
