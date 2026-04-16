from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.cost_model import DefaultCostModel, TaxLot, TaxMethod


@dataclass
class Position:
    """Position with tax lot tracking."""

    symbol: str
    quantity: int
    avg_cost: float
    tax_lots: list[TaxLot] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.quantity == 0


@dataclass
class PortfolioState:
    """In-memory portfolio state during backtest execution. Stub for SEV-276."""

    cash: float = 100_000.0
    positions: dict[str, float] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.positions

    def apply_fill(self, symbol: str, quantity: float, price: float) -> None:
        raise NotImplementedError


@dataclass
class PortfolioSnapshot:
    """Immutable snapshot of portfolio state."""

    cash: float
    positions: dict[str, dict[str, Any]]
    total_value: float
    timestamp: datetime

    def allocation_weight(self, symbol: str) -> float:
        if symbol not in self.positions or self.total_value == 0:
            return 0.0
        pos = self.positions[symbol]
        return (pos["quantity"] * pos.get("current_price", pos["avg_cost"])) / self.total_value

    def summary(self) -> str:
        return f"Cash: ${self.cash:,.2f}, Positions: {len(self.positions)}"


class Portfolio:
    def __init__(self, initial_cash: float = 100_000.0) -> None:
        self._cash = initial_cash
        self._positions: dict[str, Position] = {}
        self._trade_history: list[dict[str, Any]] = []
        self._realized_pnl: float = 0.0
        self._cost_model = DefaultCostModel()

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    @property
    def total_value(self) -> float:
        total = self._cash
        for pos in self._positions.values():
            total += pos.quantity * pos.avg_cost
        return total

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def trade_history(self) -> list[dict[str, Any]]:
        return self._trade_history

    @property
    def total_return_pct(self) -> float:
        if self.total_value == 0:
            return 0.0
        return ((self.total_value - 100_000.0) / 100_000.0) * 100

    def update_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self._positions:
                self._positions[symbol].avg_cost = price

    def open_position(
        self,
        symbol: str,
        quantity: int,
        price: float,
        cost: float = 0.0,
    ) -> None:
        total_cost = (quantity * price) + cost
        if total_cost > self._cash:
            raise ValueError(f"Insufficient cash: need {total_cost}, have {self._cash}")

        self._cash -= total_cost

        cost_per_share = price + (cost / quantity) if quantity > 0 else price
        lot = TaxLot(
            lot_id=str(uuid.uuid4()),
            symbol=symbol,
            quantity=quantity,
            purchase_price=cost_per_share,
            purchase_date=datetime.now(UTC),
        )

        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.quantity += quantity
            total_quantity = pos.quantity
            pos.avg_cost = (
                (pos.avg_cost * (pos.quantity - quantity)) + (price * quantity)
            ) / total_quantity
            pos.tax_lots.append(lot)
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                avg_cost=price,
                tax_lots=[lot],
            )

        self._trade_history.append(
            {
                "side": "buy",
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "cost": cost,
                "timestamp": datetime.now(UTC),
            }
        )

    def close_position(
        self,
        symbol: str,
        quantity: int,
        price: float,
        cost: float = 0.0,
        tax: float = 0.0,
        tax_method: TaxMethod = TaxMethod.FIFO,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if symbol not in self._positions:
            raise ValueError(f"No position for {symbol}")

        pos = self._positions[symbol]
        if quantity > pos.quantity:
            raise ValueError(
                f"Cannot sell {quantity} shares of {symbol}, only have {pos.quantity}"
            )

        remaining = quantity
        realized_gains: list[dict[str, Any]] = []
        consumed_lots: list[TaxLot] = []

        lots = self._sort_lots(pos.tax_lots, tax_method, metadata)

        for lot in lots:
            if remaining <= 0:
                break
            shares_from_lot = min(remaining, lot.quantity)

            proceeds_per_share = price - (cost / quantity) if quantity > 0 else price
            gain = (proceeds_per_share - lot.purchase_price) * shares_from_lot
            holding_days = (datetime.now(UTC) - lot.purchase_date).days
            is_long_term = holding_days >= 365

            tax_rate = (
                self._cost_model.long_term_tax_rate
                if is_long_term
                else self._cost_model.short_term_tax_rate
            )
            tax_from_gain = max(0, gain * tax_rate) if gain > 0 else 0

            realized_gains.append(
                {
                    "lot_id": lot.lot_id,
                    "shares": shares_from_lot,
                    "purchase_price": lot.purchase_price,
                    "sale_price": proceeds_per_share,
                    "gain": gain,
                    "holding_days": holding_days,
                    "is_long_term": is_long_term,
                    "tax_rate": tax_rate,
                    "tax": tax_from_gain,
                }
            )

            if shares_from_lot == lot.quantity:
                consumed_lots.append(lot)
            else:
                lot.quantity -= shares_from_lot

            remaining -= shares_from_lot

        for lot in consumed_lots:
            pos.tax_lots.remove(lot)

        total_proceeds = (quantity * price) - cost
        total_cost_basis = sum(
            l.purchase_price * realized_gains[i]["shares"]
            for i, l in enumerate(lots[: len(realized_gains)])
        )
        pnl = total_proceeds - total_cost_basis
        calculated_tax = sum(g["tax"] for g in realized_gains)

        if tax > 0:
            total_tax = tax
            pnl -= tax
        else:
            total_tax = calculated_tax
            pnl -= calculated_tax

        self._realized_pnl += pnl
        self._cash += total_proceeds

        pos.quantity -= quantity
        if pos.quantity == 0:
            del self._positions[symbol]
        else:
            pos.avg_cost = (
                (pos.avg_cost * (pos.quantity + quantity)) - (price * quantity)
            ) / pos.quantity

        self._trade_history.append(
            {
                "side": "sell",
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "cost": cost,
                "tax": total_tax,
                "pnl": pnl,
                "realized_gains": realized_gains,
                "timestamp": datetime.now(UTC),
            }
        )

        return {
            "pnl": pnl,
            "tax": total_tax,
            "realized_gains": realized_gains,
        }

    def _sort_lots(
        self,
        lots: list[TaxLot],
        method: TaxMethod,
        metadata: dict[str, Any] | None = None,
    ) -> list[TaxLot]:
        if method == TaxMethod.FIFO:
            return sorted(lots, key=lambda l: l.purchase_date)
        elif method == TaxMethod.LIFO:
            return sorted(lots, key=lambda l: l.purchase_date, reverse=True)
        elif method == TaxMethod.SPECIFIC_LOT:
            if not metadata or "lot_id" not in metadata:
                raise ValueError("specific_lot method requires lot_id in metadata")
            target_lot_id = metadata["lot_id"]
            for lot in lots:
                if lot.lot_id == target_lot_id:
                    return [lot]
            raise ValueError(f"Lot {target_lot_id} not found")
        return list(lots)

    def snapshot(self) -> PortfolioSnapshot:
        positions_dict = {}
        for symbol, pos in self._positions.items():
            positions_dict[symbol] = {
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "tax_lots": [
                    {
                        "lot_id": lot.lot_id,
                        "quantity": lot.quantity,
                        "purchase_price": lot.purchase_price,
                        "purchase_date": lot.purchase_date,
                    }
                    for lot in pos.tax_lots
                ],
                "current_price": pos.avg_cost,
            }
        return PortfolioSnapshot(
            cash=self._cash,
            positions=positions_dict,
            total_value=self.total_value,
            timestamp=datetime.now(UTC),
        )
