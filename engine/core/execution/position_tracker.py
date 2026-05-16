"""
Paper trade position tracker with real-time P&L calculation.

Tracks long/short positions, computes realized and unrealized P&L,
handles position reversals, pyramiding, and portfolio-level aggregation.
Uses integer cents internally to avoid floating-point precision drift.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from engine.core.execution.paper_broker_interface import PaperPosition

logger = structlog.get_logger()


@dataclass
class _PositionEntry:
    symbol: str
    quantity: int
    entry_price_cents: int
    realized_pnl_cents: int = 0
    current_price_cents: int = 0

    _CENTS_MULTIPLIER = 10_000

    @property
    def entry_price(self) -> float:
        return self.entry_price_cents / self._CENTS_MULTIPLIER

    @property
    def current_price(self) -> float:
        return self.current_price_cents / self._CENTS_MULTIPLIER

    @current_price.setter
    def current_price(self, value: float) -> None:
        self.current_price_cents = int(value * self._CENTS_MULTIPLIER)

    @property
    def unrealized_pnl(self) -> float:
        if self.quantity == 0:
            return 0.0
        pnl_cents = (self.current_price_cents - self.entry_price_cents) * self.quantity
        return pnl_cents / self._CENTS_MULTIPLIER

    @property
    def realized_pnl(self) -> float:
        return self.realized_pnl_cents / self._CENTS_MULTIPLIER

    @property
    def market_value(self) -> float:
        if self.quantity == 0:
            return 0.0
        return (self.current_price_cents * abs(self.quantity)) / self._CENTS_MULTIPLIER

    def to_paper_position(self) -> PaperPosition:
        return PaperPosition(
            symbol=self.symbol,
            quantity=self.quantity,
            avg_entry_price=self.entry_price,
            current_price=self.current_price,
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
            market_value=self.market_value,
        )


@dataclass
class PositionTrackerSnapshot:
    positions: dict[str, PaperPosition]
    total_unrealized_pnl: float
    total_realized_pnl: float
    total_pnl: float
    cash: float
    total_equity: float
    max_drawdown: float
    win_count: int
    loss_count: int
    total_closed_trades: int

    @property
    def win_rate(self) -> float:
        if self.total_closed_trades == 0:
            return 0.0
        return self.win_count / self.total_closed_trades


class PaperPositionTracker:
    def __init__(self, initial_cash: float = 100_000.0) -> None:
        self._M = _PositionEntry._CENTS_MULTIPLIER
        self._initial_cash_cents = int(initial_cash * self._M)
        self._cash_cents = self._initial_cash_cents
        self._positions: dict[str, _PositionEntry] = {}
        self._closed_trade_pnls: list[float] = []
        self._realized_pnl_cents = 0
        self._peak_equity_cents = self._initial_cash_cents
        self._max_drawdown = 0.0
        self._win_count = 0
        self._loss_count = 0
        self._commission_total_cents = 0

    @property
    def cash(self) -> float:
        return self._cash_cents / self._M

    @property
    def total_realized_pnl(self) -> float:
        return self._realized_pnl_cents / self._M

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def total_pnl(self) -> float:
        return self.total_realized_pnl + self.total_unrealized_pnl

    @property
    def positions_value(self) -> float:
        return sum(p.market_value for p in self._positions.values() if p.quantity != 0)

    @property
    def total_equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def max_drawdown(self) -> float:
        return self._max_drawdown

    @property
    def win_rate(self) -> float:
        total = self._win_count + self._loss_count
        if total == 0:
            return 0.0
        return self._win_count / total

    def open_or_update(
        self,
        symbol: str,
        quantity: int,
        price: float,
        commission: float = 0.0,
    ) -> PaperPosition:
        price_cents = int(price * self._M)
        commission_cents = int(commission * self._M)

        self._cash_cents -= int(quantity * price * self._M) + commission_cents
        self._commission_total_cents += commission_cents

        if symbol in self._positions:
            pos = self._positions[symbol]
            existing_qty = pos.quantity
            new_qty = existing_qty + quantity

            if (existing_qty > 0 and quantity > 0) or (existing_qty < 0 and quantity < 0):
                total_cost = abs(existing_qty) * pos.entry_price_cents + abs(quantity) * price_cents
                pos.entry_price_cents = total_cost // abs(new_qty) if new_qty != 0 else 0
                pos.quantity = new_qty
            elif new_qty == 0:
                realized = (price_cents - pos.entry_price_cents) * existing_qty
                self._realized_pnl_cents += realized
                pos.realized_pnl_cents += realized
                self._record_closed_trade(realized / self._M)
                del self._positions[symbol]
                self._update_drawdown()
                return PaperPosition(
                    symbol=symbol,
                    quantity=0,
                    avg_entry_price=price,
                    current_price=price,
                    unrealized_pnl=0.0,
                    realized_pnl=realized / self._M,
                    market_value=0.0,
                )
            else:
                closing_qty = min(abs(existing_qty), abs(quantity))
                direction = 1 if existing_qty > 0 else -1
                realized = (price_cents - pos.entry_price_cents) * direction * closing_qty
                self._realized_pnl_cents += realized
                pos.realized_pnl_cents += realized
                self._record_closed_trade(realized / self._M)

                if abs(quantity) <= abs(existing_qty):
                    pos.quantity = new_qty
                else:
                    pos.quantity = new_qty
                    pos.entry_price_cents = price_cents
        else:
            self._positions[symbol] = _PositionEntry(
                symbol=symbol,
                quantity=quantity,
                entry_price_cents=price_cents,
                current_price_cents=price_cents,
            )

        self._update_drawdown()
        return self._positions[symbol].to_paper_position() if symbol in self._positions else PaperPosition(
            symbol=symbol, quantity=0, avg_entry_price=0.0,
            current_price=price, unrealized_pnl=0.0, realized_pnl=0.0, market_value=0.0,
        )

    def close_position(
        self,
        symbol: str,
        quantity: int,
        price: float,
        commission: float = 0.0,
    ) -> PaperPosition:
        if symbol not in self._positions:
            raise ValueError(f"No position for {symbol}")

        pos = self._positions[symbol]
        close_qty = min(abs(quantity), abs(pos.quantity))
        negated = -close_qty if pos.quantity > 0 else close_qty
        return self.open_or_update(symbol, negated, price, commission)

    def update_price(self, symbol: str, price: float) -> None:
        if symbol in self._positions:
            self._positions[symbol].current_price = price
            self._update_drawdown()

    def update_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            self.update_price(symbol, price)

    def get_position(self, symbol: str) -> PaperPosition | None:
        if symbol not in self._positions:
            return None
        return self._positions[symbol].to_paper_position()

    def get_positions(self) -> dict[str, PaperPosition]:
        return {
            sym: pos.to_paper_position()
            for sym, pos in self._positions.items()
            if pos.quantity != 0
        }

    def get_snapshot(self) -> PositionTrackerSnapshot:
        positions = self.get_positions()
        equity = self.total_equity
        return PositionTrackerSnapshot(
            positions=positions,
            total_unrealized_pnl=self.total_unrealized_pnl,
            total_realized_pnl=self.total_realized_pnl,
            total_pnl=self.total_pnl,
            cash=self.cash,
            total_equity=equity,
            max_drawdown=self._max_drawdown,
            win_count=self._win_count,
            loss_count=self._loss_count,
            total_closed_trades=self._win_count + self._loss_count,
        )

    def get_position_quantity(self, symbol: str) -> int:
        if symbol not in self._positions:
            return 0
        return self._positions[symbol].quantity

    def _record_closed_trade(self, pnl: float) -> None:
        self._closed_trade_pnls.append(pnl)
        if pnl >= 0:
            self._win_count += 1
        else:
            self._loss_count += 1

    def _update_drawdown(self) -> None:
        equity_cents = int(self.total_equity * self._M)
        self._peak_equity_cents = max(self._peak_equity_cents, equity_cents)
        if self._peak_equity_cents > 0:
            dd = (self._peak_equity_cents - equity_cents) / self._peak_equity_cents
            self._max_drawdown = max(self._max_drawdown, dd)
