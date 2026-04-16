"""
Shared types used across the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, Field


@dataclass
class Money:
    amount: float
    currency: str = "USD"

    def as_pct_of(self, total: float) -> float:
        if total == 0:
            return 0.0
        return (self.amount / total) * 100


@dataclass
class CostBreakdown:
    commission: Money = field(default_factory=lambda: Money(0.0))
    spread: Money = field(default_factory=lambda: Money(0.0))
    slippage: Money = field(default_factory=lambda: Money(0.0))
    exchange_fee: Money = field(default_factory=lambda: Money(0.0))
    tax_estimate: Money = field(default_factory=lambda: Money(0.0))

    @property
    def total(self) -> Money:
        return Money(
            amount=(
                self.commission.amount + self.spread.amount +
                self.slippage.amount + self.exchange_fee.amount +
                self.tax_estimate.amount
            )
        )


class PortfolioSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cash: float = 0.0
    positions: dict[str, dict] = Field(default_factory=dict)
    total_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    day_pnl: float = 0.0
    total_return_pct: float = 0.0

    def get_position(self, symbol: str) -> dict | None:
        return self.positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def allocation_weight(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        if not pos or self.total_value == 0:
            return 0.0
        return pos.get("market_value", 0) / self.total_value

    def summary(self) -> str:
        return f"NAV: ${self.total_value:,.2f} | Cash: ${self.cash:,.2f} | Positions: {len(self.positions)}"
