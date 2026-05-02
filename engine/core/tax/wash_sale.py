"""US IRS Section 1091 wash-sale detector (gh#156).

A "wash sale" is a sale of a security at a loss when the same (or a
substantially identical) security is bought within a ±30-day window
around the sale. The disallowed loss is added to the cost basis of
the replacement shares; the holding period of the replacement
inherits from the lot that was sold.

This module is intentionally a *pure function* over a stream of
trades. It does not read the database. The caller passes a list of
:class:`Trade` records (sourced from fills, lot movements, or a
backtest); the detector returns a list of
:class:`WashSaleAdjustment` records that the caller can persist or
report on.

Scope
-----
- "Substantially identical" is implemented as exact symbol match.
  Real-world identification (options, ETF/ETF substitutes,
  reorganisations) is a follow-up that lives behind a configurable
  hook.
- Lot selection is FIFO within the same symbol when the caller does
  not supply per-sale cost basis.
- Mark-to-market trades (Section 1256 contracts) are out of scope.
- Cross-account / cross-portfolio aggregation is out of scope (the
  caller decides what set of trades to feed in).

Reference: 26 U.S.C. § 1091; IRS Pub. 550, "Investment Income and
Expenses".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

WASH_SALE_WINDOW_DAYS: int = 30


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Trade:
    """One executed trade in a single security.

    ``trade_id`` is opaque; the caller maps it back to whatever
    persistence layer it uses. ``quantity`` and ``price`` are
    ``Decimal`` so basis arithmetic stays exact.
    """

    trade_id: str
    symbol: str
    side: TradeSide
    quantity: Decimal
    price: Decimal
    when: datetime

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"trade {self.trade_id!r}: quantity must be positive")
        if self.price < 0:
            raise ValueError(f"trade {self.trade_id!r}: price must be non-negative")

    @property
    def gross(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class WashSaleAdjustment:
    """One disallowed-loss adjustment.

    The caller adds ``disallowed_loss`` to the cost basis of
    ``replacement_trade_id``'s lot. ``matched_quantity`` is the
    number of shares the replacement absorbed from the loss sale —
    the disallowed-loss amount is already prorated to that quantity.
    """

    sale_trade_id: str
    replacement_trade_id: str
    symbol: str
    matched_quantity: Decimal
    disallowed_loss: Decimal


@dataclass
class _BuyState:
    """Mutable buy-side accounting carried across the scan."""

    trade: Trade
    available_qty: Decimal


@dataclass
class _SaleState:
    """Mutable sale-side accounting carried across the scan."""

    trade: Trade
    cost_basis: Decimal
    total_loss: Decimal  # full loss at sale time, fixed
    remaining_qty: Decimal
    remaining_loss: Decimal  # always non-negative; only loss sales tracked


def detect_wash_sales(
    trades: list[Trade],
    *,
    cost_basis_for: dict[str, Decimal] | None = None,
) -> list[WashSaleAdjustment]:
    """Return wash-sale adjustments implied by ``trades``.

    ``cost_basis_for`` is an optional ``trade_id -> total cost basis``
    map for sale trades. If a sale's ``trade_id`` is missing, the
    detector falls back to using FIFO matching against earlier buys
    in ``trades`` to compute the basis.

    The output is sorted by ``(sale_trade_id, replacement_trade_id)``
    so callers can diff against previous runs.
    """
    sales = [t for t in trades if t.side == TradeSide.SELL]

    # FIFO state shared between basis computation and wash-sale matching.
    # After basis consumption, lots' ``available_qty`` reflects shares
    # still held — those (and only those) can be wash-sale replacements.
    fifo_lots: dict[str, list[_BuyState]] = {}
    cost_basis_for = dict(cost_basis_for or {})
    for trade in sorted(trades, key=lambda t: t.when):
        if trade.side == TradeSide.BUY:
            fifo_lots.setdefault(trade.symbol, []).append(
                _BuyState(trade=trade, available_qty=trade.quantity)
            )
        else:
            consumed_basis = _consume_fifo(
                fifo_lots.get(trade.symbol, []), trade.quantity
            )
            if trade.trade_id not in cost_basis_for:
                cost_basis_for[trade.trade_id] = consumed_basis

    # Build sale states; only sales at a loss matter.
    sale_states: list[_SaleState] = []
    for s in sales:
        basis = cost_basis_for.get(s.trade_id, Decimal("0"))
        loss = basis - s.gross  # positive = loss
        if loss <= 0:
            continue
        sale_states.append(
            _SaleState(
                trade=s,
                cost_basis=basis,
                total_loss=loss,
                remaining_qty=s.quantity,
                remaining_loss=loss,
            )
        )
    sale_states.sort(key=lambda s: s.trade.when)

    # Wash-sale candidates = lots with surviving ``available_qty``.
    # Order by time so earlier replacements consume the loss first.
    buy_states = sorted(
        (
            lot
            for lots in fifo_lots.values()
            for lot in lots
            if lot.available_qty > 0
        ),
        key=lambda b: b.trade.when,
    )

    adjustments: list[WashSaleAdjustment] = []
    window = timedelta(days=WASH_SALE_WINDOW_DAYS)

    for sale in sale_states:
        for buy in buy_states:
            if buy.available_qty <= 0:
                continue
            if buy.trade.symbol != sale.trade.symbol:
                continue
            if abs(buy.trade.when - sale.trade.when) > window:
                continue
            if buy.trade.trade_id == sale.trade.trade_id:
                # Defensive: a single trade should never be both buy
                # and sale, but if the caller passed bad data don't
                # match it against itself.
                continue

            matched = min(sale.remaining_qty, buy.available_qty)
            if matched <= 0:
                continue
            # Prorate the FULL loss by matched_qty / sale.qty so multiple
            # replacements consume the loss linearly (not exponentially).
            disallowed = (
                sale.total_loss * (matched / sale.trade.quantity)
            ).quantize(Decimal("0.0001"))
            adjustments.append(
                WashSaleAdjustment(
                    sale_trade_id=sale.trade.trade_id,
                    replacement_trade_id=buy.trade.trade_id,
                    symbol=sale.trade.symbol,
                    matched_quantity=matched,
                    disallowed_loss=disallowed,
                )
            )

            sale.remaining_qty -= matched
            sale.remaining_loss -= disallowed
            buy.available_qty -= matched

            if sale.remaining_qty <= 0 or sale.remaining_loss <= 0:
                break

    adjustments.sort(
        key=lambda a: (a.sale_trade_id, a.replacement_trade_id)
    )
    return adjustments


def _consume_fifo(lots: list[_BuyState], qty: Decimal) -> Decimal:
    """FIFO-consume ``qty`` shares from ``lots`` and return cost basis.

    Mutates ``lots`` so that subsequent calls see the reduced
    availability. If ``lots`` doesn't have enough, the missing portion
    contributes zero basis. Real systems should reject that case
    upstream — this is just a fallback so the detector doesn't crash
    on partial inputs.
    """
    remaining = qty
    basis = Decimal("0")
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.available_qty, remaining)
        if take <= 0:
            continue
        basis += take * lot.trade.price
        lot.available_qty -= take
        remaining -= take
    return basis
