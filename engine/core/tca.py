"""Post-trade Transaction Cost Analysis.

Computes implementation shortfall (IS) and arrival slippage per fill,
plus aggregations over an arbitrary list of fills with rollups by
broker and by symbol.

Definitions
-----------
- *Decision price*: the price at the moment the strategy decided to
  trade. The signal's reference quote.
- *Arrival price*: the price at the moment the parent order arrived
  at the venue. Captures pre-trade market drift and queue position.
- *Fill price*: the actual execution price.

For a BUY:
    implementation_shortfall = (fill_price - decision_price) * quantity + fees
    slippage_vs_arrival     = (fill_price - arrival_price)  * quantity

For a SELL the price differences flip sign so a positive number always
means "we paid more than expected" / "we sold for less than expected".
Both quantities are reported as positive cost in dollars and as basis
points of the fill notional.

Pure-Python over plain dataclasses; no pandas/numpy dependency.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Fill:
    """One executed trade record."""

    symbol: str
    side: Side
    quantity: int
    fill_price: float
    decision_price: float
    arrival_price: float
    fees: float
    broker: str

    def __post_init__(self) -> None:
        for name, value in (
            ("fill_price", self.fill_price),
            ("decision_price", self.decision_price),
            ("arrival_price", self.arrival_price),
            ("fees", self.fees),
        ):
            if not math.isfinite(value):
                msg = f"{name} must be finite; got {value}"
                raise ValueError(msg)
        if self.quantity <= 0:
            msg = f"quantity must be positive; got {self.quantity}"
            raise ValueError(msg)
        if self.fill_price <= 0:
            msg = f"fill_price must be positive; got {self.fill_price}"
            raise ValueError(msg)
        if self.decision_price <= 0 or self.arrival_price <= 0:
            msg = "decision_price and arrival_price must be positive"
            raise ValueError(msg)
        if self.fees < 0:
            msg = f"fees must be non-negative; got {self.fees}"
            raise ValueError(msg)


@dataclass(frozen=True)
class FillMetrics:
    """Per-fill TCA outputs."""

    implementation_shortfall: float
    slippage_vs_arrival: float
    fees: float
    notional: float
    implementation_shortfall_bps: float
    slippage_vs_arrival_bps: float


def _signed_cost(side: Side, fill_price: float, ref_price: float) -> float:
    """Cost in price terms — always positive when fill is unfavorable."""
    if side == Side.BUY:
        return fill_price - ref_price
    return ref_price - fill_price


def fill_metrics(fill: Fill) -> FillMetrics:
    notional = fill.quantity * fill.fill_price
    is_per_share = _signed_cost(fill.side, fill.fill_price, fill.decision_price)
    slip_per_share = _signed_cost(fill.side, fill.fill_price, fill.arrival_price)
    is_total = is_per_share * fill.quantity + fill.fees
    slip_total = slip_per_share * fill.quantity
    if notional > 0:
        is_bps = is_total / notional * 10_000.0
        slip_bps = slip_total / notional * 10_000.0
    else:
        is_bps = 0.0
        slip_bps = 0.0
    return FillMetrics(
        implementation_shortfall=is_total,
        slippage_vs_arrival=slip_total,
        fees=fill.fees,
        notional=notional,
        implementation_shortfall_bps=is_bps,
        slippage_vs_arrival_bps=slip_bps,
    )


@dataclass(frozen=True)
class TCAReport:
    """Aggregate TCA report over a list of fills."""

    total_implementation_shortfall: float = 0.0
    total_slippage_vs_arrival: float = 0.0
    total_fees: float = 0.0
    total_notional: float = 0.0
    fill_count: int = 0
    weighted_average_is_bps: float = 0.0
    weighted_average_slippage_bps: float = 0.0
    by_broker: dict[str, TCAReport] = field(default_factory=dict)
    by_symbol: dict[str, TCAReport] = field(default_factory=dict)


def _empty_report() -> TCAReport:
    return TCAReport()


def _aggregate_no_rollups(fills: list[Fill]) -> TCAReport:
    """Aggregate without per-broker / per-symbol rollups (used for nested)."""
    if not fills:
        return _empty_report()
    is_total = 0.0
    slip_total = 0.0
    fee_total = 0.0
    notional_total = 0.0
    for f in fills:
        m = fill_metrics(f)
        is_total += m.implementation_shortfall
        slip_total += m.slippage_vs_arrival
        fee_total += m.fees
        notional_total += m.notional
    if notional_total > 0:
        wa_is = is_total / notional_total * 10_000.0
        wa_slip = slip_total / notional_total * 10_000.0
    else:
        wa_is = 0.0
        wa_slip = 0.0
    return TCAReport(
        total_implementation_shortfall=is_total,
        total_slippage_vs_arrival=slip_total,
        total_fees=fee_total,
        total_notional=notional_total,
        fill_count=len(fills),
        weighted_average_is_bps=wa_is,
        weighted_average_slippage_bps=wa_slip,
    )


def aggregate_tca(fills: Iterable[Fill]) -> TCAReport:
    """Compute the full TCA report including per-broker + per-symbol rollups."""
    fills_list = list(fills)
    if not fills_list:
        return _empty_report()
    base = _aggregate_no_rollups(fills_list)
    by_broker_groups: dict[str, list[Fill]] = defaultdict(list)
    by_symbol_groups: dict[str, list[Fill]] = defaultdict(list)
    for f in fills_list:
        by_broker_groups[f.broker].append(f)
        by_symbol_groups[f.symbol].append(f)
    by_broker = {
        broker: _aggregate_no_rollups(group) for broker, group in by_broker_groups.items()
    }
    by_symbol = {
        symbol: _aggregate_no_rollups(group) for symbol, group in by_symbol_groups.items()
    }
    return TCAReport(
        total_implementation_shortfall=base.total_implementation_shortfall,
        total_slippage_vs_arrival=base.total_slippage_vs_arrival,
        total_fees=base.total_fees,
        total_notional=base.total_notional,
        fill_count=base.fill_count,
        weighted_average_is_bps=base.weighted_average_is_bps,
        weighted_average_slippage_bps=base.weighted_average_slippage_bps,
        by_broker=by_broker,
        by_symbol=by_symbol,
    )


__all__ = [
    "Fill",
    "FillMetrics",
    "Side",
    "TCAReport",
    "aggregate_tca",
    "fill_metrics",
]
