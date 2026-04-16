from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from engine.core.cost_model import DefaultCostModel, TaxLot, TaxMethod


@dataclass
class Position:
    """Represents a position in a single security."""

    symbol: str
    quantity: Decimal = Decimal("0")
    avg_cost: float = 0.0

    @property
    def is_zero(self) -> bool:
        return self.quantity == 0


@dataclass
class TradeRecord:
    """Record of a single trade."""

    timestamp: datetime
    side: str
    symbol: str
    quantity: Decimal
    price: float
    cost: float = 0.0
    tax: float = 0.0
    lot_ids: list[str] = field(default_factory=list)


@dataclass
class Portfolio:
    """Portfolio with full tax lot tracking for accurate capital gains calculation."""

    initial_cash: float = 100_000.0
    _cash: float = field(default=100_000.0)
    positions: dict[str, Position] = field(default_factory=dict)
    _tax_lots: dict[str, list[TaxLot]] = field(default_factory=dict)
    trade_history: list[TradeRecord] = field(default_factory=list)
    realized_pnl: float = 0.0
    tax_method: TaxMethod = TaxMethod.FIFO
    _cost_model: DefaultCostModel | None = field(default=None)
    portfolio_id: uuid.UUID | None = None
    transaction_date: datetime | None = None

    def __post_init__(self):
        self._cash = self.initial_cash
        if self._cost_model is None:
            self._cost_model = DefaultCostModel()

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def total_value(self) -> float:
        positions_value = sum(
            float(pos.quantity) * pos.avg_cost for pos in self.positions.values()
        )
        return self._cash + positions_value

    @property
    def total_return_pct(self) -> float:
        if self.initial_cash == 0:
            return 0.0
        return ((self.total_value - self.initial_cash) / self.initial_cash) * 100

    def open_position(
        self,
        symbol: str,
        quantity: int,
        price: float,
        cost: float = 0.0,
    ) -> uuid.UUID:
        """Open a new position (buy). Creates a new tax lot."""
        total_cost = (quantity * price) + cost

        if self._cash < total_cost:
            raise ValueError(f"Insufficient cash: need {total_cost}, have {self._cash}")

        self._cash -= total_cost

        lot_id = str(uuid.uuid4())
        purchase_date = self.transaction_date or datetime.now(UTC)

        lot = TaxLot(
            lot_id=lot_id,
            symbol=symbol,
            quantity=quantity,
            purchase_price=price,
            purchase_date=purchase_date,
        )

        if symbol not in self._tax_lots:
            self._tax_lots[symbol] = []
        self._tax_lots[symbol].append(lot)

        if symbol in self.positions:
            existing = self.positions[symbol]
            total_quantity = existing.quantity + Decimal(str(quantity))
            total_cost_basis = float(existing.quantity) * existing.avg_cost + (quantity * price)
            existing.avg_cost = float(total_cost_basis / float(total_quantity))
            existing.quantity = total_quantity
        else:
            self.positions[symbol] = Position(
                symbol=symbol,
                quantity=Decimal(str(quantity)),
                avg_cost=price,
            )

        self.trade_history.append(
            TradeRecord(
                timestamp=purchase_date,
                side="buy",
                symbol=symbol,
                quantity=Decimal(str(quantity)),
                price=price,
                cost=cost,
                lot_ids=[lot_id],
            )
        )

        return uuid.UUID(lot_id)

    def close_position(
        self,
        symbol: str,
        quantity: int,
        price: float,
        cost: float = 0.0,
        tax: float = 0.0,
    ) -> list[dict]:
        """Close position (sell), consuming tax lots in FIFO/LIFO order."""
        if symbol not in self.positions:
            raise ValueError(f"No position for {symbol}")

        position = self.positions[symbol]
        if Decimal(str(quantity)) > position.quantity:
            raise ValueError(
                f"Cannot sell {quantity} shares of {symbol}, only {position.quantity} held"
            )

        lots = self._tax_lots.get(symbol, [])
        if not lots:
            raise ValueError(f"No tax lots found for {symbol}")

        if self.tax_method == TaxMethod.FIFO:
            sorted_lots = sorted(lots, key=lambda l: l.purchase_date)
        elif self.tax_method == TaxMethod.LIFO:
            sorted_lots = sorted(lots, key=lambda l: l.purchase_date, reverse=True)
        else:
            sorted_lots = lots

        remaining_to_sell = Decimal(str(quantity))
        consumed_lots = []
        total_cost_basis = Decimal("0")

        sell_date = self.transaction_date or datetime.now(UTC)

        for lot in sorted_lots:
            if remaining_to_sell <= 0:
                break

            consumed_qty = min(remaining_to_sell, Decimal(str(lot.quantity)))
            lot_cost_basis = consumed_qty * Decimal(str(lot.purchase_price))
            total_cost_basis += lot_cost_basis

            consumed_lots.append(
                {
                    "lot_id": lot.lot_id,
                    "quantity": int(consumed_qty),
                    "purchase_price": lot.purchase_price,
                    "purchase_date": lot.purchase_date,
                    "is_long_term": lot.is_long_term(as_of=sell_date),
                }
            )

            remaining_to_sell -= consumed_qty
            lot.quantity = int(Decimal(str(lot.quantity)) - consumed_qty)

            if lot.quantity <= 0:
                lots.remove(lot)

        if symbol in self.positions:
            position.quantity -= Decimal(str(quantity))
            if position.quantity <= 0:
                del self.positions[symbol]

        self._cash += float(Decimal(str(quantity)) * Decimal(str(price))) - cost

        sell_value = float(Decimal(str(quantity)) * Decimal(str(price)))
        cost_basis = float(total_cost_basis)
        gain = sell_value - cost_basis - cost - tax
        self.realized_pnl += gain

        sell_date = self.transaction_date or datetime.now(UTC)
        buy_history = [{"symbol": symbol, "date": lot.purchase_date} for lot in sorted_lots]
        wash_sale = (
            self._cost_model.check_wash_sale(symbol, sell_date, buy_history) if gain < 0 else False
        )

        if wash_sale:
            tax = 0.0

        self.trade_history.append(
            TradeRecord(
                timestamp=sell_date,
                side="sell",
                symbol=symbol,
                quantity=Decimal(str(quantity)),
                price=price,
                cost=cost,
                tax=tax,
                lot_ids=[c["lot_id"] for c in consumed_lots],
            )
        )

        return consumed_lots

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current prices for positions (for P&L calculation)."""
        for symbol, price in prices.items():
            if symbol in self.positions:
                self.positions[symbol].avg_cost = price

    def snapshot(self) -> PortfolioSnapshot:
        """Create an immutable snapshot of current portfolio state."""
        return PortfolioSnapshot(
            cash=self._cash,
            positions={
                symbol: {"quantity": pos.quantity, "avg_cost": pos.avg_cost}
                for symbol, pos in self.positions.items()
            },
            total_value=self.total_value,
            total_return_pct=self.total_return_pct,
            realized_pnl=self.realized_pnl,
        )

    def get_tax_lots(self, symbol: str) -> list[TaxLot]:
        """Get all tax lots for a symbol."""
        return self._tax_lots.get(symbol, [])

    def set_tax_method(self, method: TaxMethod) -> None:
        """Set the tax lot accounting method (FIFO, LIFO, or SPECIFIC_LOT)."""
        self.tax_method = method


@dataclass
class PortfolioSnapshot:
    """Immutable snapshot of portfolio state."""

    cash: float
    positions: dict[str, dict]
    total_value: float
    total_return_pct: float
    realized_pnl: float

    def allocation_weight(self, symbol: str) -> float:
        if symbol not in self.positions or self.total_value == 0:
            return 0.0
        position_value = (
            float(self.positions[symbol]["quantity"]) * self.positions[symbol]["avg_cost"]
        )
        return (position_value / self.total_value) * 100

    def summary(self) -> str:
        return f"Cash: ${self.cash:,.0f}, Value: ${self.total_value:,.0f}, Return: {self.total_return_pct:.2f}%"
