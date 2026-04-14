"""
Portfolio — tracks positions, cash, NAV, P&L, and tax lots.

Provides the PortfolioSnapshot that strategies receive in evaluate().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from core.cost_model import TaxLot, Money


@dataclass
class Position:
    """A single open position in the portfolio."""
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    tax_lots: list[TaxLot] = field(default_factory=list)

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_basis) * 100

    def add_lot(self, quantity: int, price: float, date: Optional[datetime] = None):
        lot = TaxLot(
            symbol=self.symbol,
            quantity=quantity,
            purchase_price=price,
            purchase_date=date or datetime.now(timezone.utc),
        )
        self.tax_lots.append(lot)
        # Recalculate average cost
        total_cost = sum(l.cost_basis for l in self.tax_lots)
        total_qty = sum(l.quantity for l in self.tax_lots)
        self.avg_cost = total_cost / total_qty if total_qty > 0 else 0
        self.quantity = total_qty


class PortfolioSnapshot(BaseModel):
    """
    Immutable snapshot passed to strategy evaluate() calls.

    Strategies cannot modify the portfolio — they can only read this
    snapshot and emit signals based on what they see.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cash: float = Field(default=0.0, description="Available cash balance")
    positions: dict[str, dict] = Field(default_factory=dict, description="Symbol -> position data")
    total_value: float = Field(default=0.0, description="NAV = cash + sum(market_values)")
    realized_pnl: float = Field(default=0.0, description="Cumulative realized P&L")
    unrealized_pnl: float = Field(default=0.0, description="Current unrealized P&L")
    day_pnl: float = Field(default=0.0, description="Today's P&L")
    total_return_pct: float = Field(default=0.0, description="Total return since inception")

    def get_position(self, symbol: str) -> Optional[dict]:
        return self.positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def allocation_weight(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        if not pos or self.total_value == 0:
            return 0.0
        return pos["market_value"] / self.total_value

    def summary(self) -> str:
        return (
            f"NAV: ${self.total_value:,.2f} | "
            f"Cash: ${self.cash:,.2f} | "
            f"Positions: {len(self.positions)} | "
            f"Return: {self.total_return_pct:+.2f}%"
        )


class Portfolio:
    """
    Mutable portfolio state managed by the engine.
    Generates immutable snapshots for strategies.
    """

    def __init__(self, initial_cash: float = 100_000.0, name: str = "Default"):
        self.name = name
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.trade_history: list[dict] = []
        self.created_at = datetime.now(timezone.utc)

    @property
    def total_value(self) -> float:
        market_value = sum(p.market_value for p in self.positions.values())
        return self.cash + market_value

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def total_return_pct(self) -> float:
        if self.initial_cash == 0:
            return 0.0
        return ((self.total_value - self.initial_cash) / self.initial_cash) * 100

    def update_prices(self, prices: dict[str, float]):
        """Update current market prices for all positions."""
        for symbol, price in prices.items():
            if symbol in self.positions:
                self.positions[symbol].current_price = price

    def open_position(self, symbol: str, quantity: int, price: float, cost: float = 0.0):
        """Open or add to a position. Deducts cash including costs."""
        total_deduction = (quantity * price) + cost
        if total_deduction > self.cash:
            raise ValueError(f"Insufficient cash: need ${total_deduction:.2f}, have ${self.cash:.2f}")

        self.cash -= total_deduction

        if symbol in self.positions:
            self.positions[symbol].add_lot(quantity, price)
        else:
            pos = Position(symbol=symbol, quantity=quantity, avg_cost=price, current_price=price)
            pos.add_lot(quantity, price)
            self.positions[symbol] = pos

        self.trade_history.append({
            "timestamp": datetime.now(timezone.utc),
            "symbol": symbol,
            "side": "buy",
            "quantity": quantity,
            "price": price,
            "cost": cost,
        })

    def close_position(self, symbol: str, quantity: int, price: float, cost: float = 0.0, tax: float = 0.0):
        """Close or reduce a position. Adds proceeds minus costs and tax to cash."""
        if symbol not in self.positions:
            raise ValueError(f"No position in {symbol}")

        pos = self.positions[symbol]
        if quantity > pos.quantity:
            raise ValueError(f"Cannot sell {quantity} shares of {symbol}, only hold {pos.quantity}")

        proceeds = (quantity * price) - cost - tax
        realized = (price - pos.avg_cost) * quantity - cost - tax
        self.cash += proceeds
        self.realized_pnl += realized

        pos.quantity -= quantity
        if pos.quantity == 0:
            del self.positions[symbol]

        self.trade_history.append({
            "timestamp": datetime.now(timezone.utc),
            "symbol": symbol,
            "side": "sell",
            "quantity": quantity,
            "price": price,
            "cost": cost,
            "tax": tax,
            "realized_pnl": realized,
        })

    def snapshot(self) -> PortfolioSnapshot:
        """Create an immutable snapshot for strategy consumption."""
        positions_data = {}
        for sym, pos in self.positions.items():
            positions_data[sym] = {
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "unrealized_pnl": pos.unrealized_pnl,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
            }

        return PortfolioSnapshot(
            cash=self.cash,
            positions=positions_data,
            total_value=self.total_value,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            total_return_pct=self.total_return_pct,
        )
