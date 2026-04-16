from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    quantity: float
    avg_price: float
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price


@dataclass
class Portfolio:
    cash: float = 100_000.0
    positions: dict[str, Position] = field(default_factory=dict)
    initial_cash: float = 100_000.0

    @property
    def is_empty(self) -> bool:
        return not self.positions

    @property
    def total_value(self) -> float:
        positions_value = sum(pos.market_value for pos in self.positions.values())
        return self.cash + positions_value

    def open_position(self, symbol: str, quantity: float, price: float, cost: float) -> None:
        self.cash -= cost
        if symbol in self.positions:
            existing = self.positions[symbol]
            total_qty = existing.quantity + quantity
            total_cost = (existing.avg_price * existing.quantity) + (price * quantity)
            self.positions[symbol] = Position(
                quantity=total_qty,
                avg_price=total_cost / total_qty if total_qty > 0 else 0,
            )
        else:
            self.positions[symbol] = Position(quantity=quantity, avg_price=price)

    def close_position(
        self, symbol: str, quantity: float, price: float, cost: float, tax: float
    ) -> None:
        total_cost = cost + tax
        self.cash += (price * quantity) - total_cost
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.quantity -= quantity
            if pos.quantity <= 0:
                del self.positions[symbol]

    def update_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self.positions:
                self.positions[symbol].current_price = price


@dataclass
class PortfolioState:
    cash: float = 100_000.0
    positions: dict[str, float] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.positions
