from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from engine.core.cost_model import DefaultCostModel, TaxLot, TaxMethod


@dataclass
class Position:
    """Represents a position in a single security."""

    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0

    @property
    def is_zero(self) -> bool:
        return self.quantity == 0

    @property
    def market_value(self) -> float:
        price = self.current_price if self.current_price > 0 else self.avg_cost
        return self.quantity * price


@dataclass
class TradeRecord:
    """Record of a single trade."""

    timestamp: datetime
    side: str
    symbol: str
    quantity: int
    price: float
    cost: float = 0.0
    tax: float = 0.0
    lot_ids: list[str] = field(default_factory=list)


@dataclass
class SellRecord:
    """Record of a sell for wash sale detection."""

    symbol: str
    sell_date: datetime
    quantity: int
    sell_price: float
    cost_basis: float
    gain: float
    remaining_disallowed: float = 0.0


@dataclass
class Portfolio:
    """Portfolio with full tax lot tracking for accurate capital gains calculation."""

    initial_cash: float = 100_000.0
    _cash: float = field(default=100_000.0)
    positions: dict[str, Position] = field(default_factory=dict)
    _tax_lots: dict[str, list[TaxLot]] = field(default_factory=dict)
    trade_history: list[TradeRecord] = field(default_factory=list)
    _sell_history: list[SellRecord] = field(default_factory=list)
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
        positions_value = sum(pos.market_value for pos in self.positions.values())
        return self._cash + positions_value

    @property
    def total_return_pct(self) -> float:
        if self.initial_cash == 0:
            return 0.0
        return ((self.total_value - self.initial_cash) / self.initial_cash) * 100

    def _consume_lots(
        self,
        symbol: str,
        quantity: int,
        sell_date: datetime,
    ) -> tuple[list[dict], float]:
        lots = self._tax_lots.get(symbol, [])
        if not lots:
            raise ValueError(f"No tax lots found for {symbol}")

        if self.tax_method == TaxMethod.FIFO:
            sorted_lots = sorted(lots, key=lambda lot: lot.purchase_date)
        elif self.tax_method == TaxMethod.LIFO:
            sorted_lots = sorted(lots, key=lambda lot: lot.purchase_date, reverse=True)
        else:
            sorted_lots = list(lots)

        remaining = quantity
        consumed: list[dict] = []
        total_cost_basis = 0.0

        i = 0
        while remaining > 0 and i < len(sorted_lots):
            lot = sorted_lots[i]
            consumed_qty = min(remaining, lot.quantity)
            lot_cost_basis = consumed_qty * lot.purchase_price
            total_cost_basis += lot_cost_basis

            consumed.append(
                {
                    "lot_id": lot.lot_id,
                    "quantity": consumed_qty,
                    "purchase_price": lot.purchase_price,
                    "purchase_date": lot.purchase_date,
                    "is_long_term": lot.is_long_term(as_of=sell_date),
                }
            )

            remaining -= consumed_qty
            lot.quantity -= consumed_qty

            if lot.quantity <= 0:
                lots.remove(lot)
            i += 1

        if remaining > 0:
            raise ValueError(f"Tax lots insufficient: {remaining} shares unfulfilled for {symbol}")

        return consumed, total_cost_basis

    def open_position(
        self,
        symbol: str,
        quantity: int,
        price: float,
        cost: float = 0.0,
    ) -> uuid.UUID:
        """Open a new position (buy). Creates a new tax lot.

        If this buy is within 30 days of a sell at a loss for the same symbol,
        the wash sale rule applies: the disallowed loss is added to this lot's
        cost basis (per IRS Pub 550).
        """
        total_cost = (quantity * price) + cost

        if self._cash < total_cost:
            raise ValueError(f"Insufficient cash: need {total_cost}, have {self._cash}")

        purchase_date = self.transaction_date or datetime.now(UTC)

        adjusted_price = price
        total_adjustment = 0.0

        for sell in self._sell_history:
            if sell.symbol != symbol:
                continue
            if sell.remaining_disallowed <= 0:
                continue
            window_start = purchase_date - timedelta(days=self._cost_model.wash_sale_window_days)
            if window_start <= sell.sell_date <= purchase_date:
                per_share = abs(sell.gain) / sell.quantity
                applicable_shares = min(quantity, sell.quantity)
                applicable = applicable_shares * per_share
                applicable = min(applicable, sell.remaining_disallowed)
                total_adjustment += applicable
                sell.remaining_disallowed -= applicable

        if total_adjustment > 0:
            adjusted_price = price + (total_adjustment / quantity)

        self._cash -= total_cost

        lot_id = str(uuid.uuid4())
        lot = TaxLot(
            lot_id=lot_id,
            symbol=symbol,
            quantity=quantity,
            purchase_price=adjusted_price,
            purchase_date=purchase_date,
        )

        if symbol not in self._tax_lots:
            self._tax_lots[symbol] = []
        self._tax_lots[symbol].append(lot)

        if symbol in self.positions:
            existing = self.positions[symbol]
            total_qty = existing.quantity + quantity
            total_cost_basis = existing.quantity * existing.avg_cost + (quantity * adjusted_price)
            existing.avg_cost = total_cost_basis / total_qty
            existing.quantity = total_qty
        else:
            self.positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                avg_cost=adjusted_price,
            )

        self.trade_history.append(
            TradeRecord(
                timestamp=purchase_date,
                side="buy",
                symbol=symbol,
                quantity=quantity,
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
        if quantity > position.quantity:
            raise ValueError(
                f"Cannot sell {quantity} shares of {symbol}, only {position.quantity} held"
            )

        sell_date = self.transaction_date or datetime.now(UTC)

        consumed_lots, total_cost_basis = self._consume_lots(symbol, quantity, sell_date)

        if symbol in self.positions:
            position.quantity -= quantity
            if position.quantity <= 0:
                del self.positions[symbol]

        sell_proceeds = quantity * price
        self._cash += sell_proceeds - cost - tax

        gain = sell_proceeds - total_cost_basis - cost
        self.realized_pnl += gain

        if gain < 0:
            adjustment = self._apply_buy_then_sell_wash_sale(symbol, sell_date, gain, quantity)
            self.realized_pnl += adjustment

        self._sell_history.append(
            SellRecord(
                symbol=symbol,
                sell_date=sell_date,
                quantity=quantity,
                sell_price=price,
                cost_basis=total_cost_basis,
                gain=gain,
                remaining_disallowed=abs(gain) if gain < 0 else 0.0,
            )
        )

        self.trade_history.append(
            TradeRecord(
                timestamp=sell_date,
                side="sell",
                symbol=symbol,
                quantity=quantity,
                price=price,
                cost=cost,
                tax=tax,
                lot_ids=[c["lot_id"] for c in consumed_lots],
            )
        )

        return consumed_lots

    def _apply_buy_then_sell_wash_sale(
        self, symbol: str, sell_date: datetime, loss: float, sold_quantity: int
    ) -> float:
        remaining_lots = self._tax_lots.get(symbol, [])
        window_start = sell_date - timedelta(days=self._cost_model.wash_sale_window_days)
        disallowed_per_share = abs(loss) / sold_quantity
        total_disallowed = 0.0
        for lot in remaining_lots:
            if window_start <= lot.purchase_date <= sell_date:
                lot.purchase_price += disallowed_per_share
                total_disallowed += lot.quantity * disallowed_per_share
        return total_disallowed

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current market prices for positions (for P&L calculation)."""
        for symbol, price in prices.items():
            if symbol in self.positions:
                self.positions[symbol].current_price = price

    def snapshot(self) -> PortfolioSnapshot:
        """Create an immutable snapshot of current portfolio state."""
        return PortfolioSnapshot(
            cash=self._cash,
            positions={
                symbol: {
                    "quantity": pos.quantity,
                    "avg_cost": pos.avg_cost,
                    "current_price": pos.current_price,
                }
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
        pos = self.positions[symbol]
        price = pos.get("current_price") or pos.get("avg_cost", 0)
        position_value = pos["quantity"] * price
        return (position_value / self.total_value) * 100

    def summary(self) -> str:
        return (
            f"Cash: ${self.cash:,.0f}, "
            f"Value: ${self.total_value:,.0f}, "
            f"Return: {self.total_return_pct:.2f}%"
        )


PortfolioState = PortfolioSnapshot  # deprecated: use PortfolioSnapshot directly
