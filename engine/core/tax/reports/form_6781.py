"""IRS Form 6781 — Section 1256 Contracts (gh#155).

Section 1256 contracts (regulated futures, foreign currency contracts,
non-equity options, dealer equity options, dealer securities futures)
are treated under a special regime:

- Mark-to-market at year-end: open contracts are deemed sold at FMV.
- 60 % long-term + 40 % short-term split applied to the *aggregate*
  net gain or loss, regardless of actual holding period.
- The split totals flow into Schedule D Part I (short-term) and
  Part II (long-term).

This module aggregates per-contract gain/loss into the two split
totals and serialises a Form-6781-shaped CSV.

Mark-to-market input
--------------------
The caller passes :class:`Section1256Contract` records that already
encode either a closed-out gain/loss or a year-end mark-to-market
gain/loss. ``proceeds_or_fmv`` and ``cost`` are signed amounts in
USD; the gain/loss for the contract is ``proceeds_or_fmv - cost``.

What's NOT here (explicit follow-ups)
-------------------------------------
- Loss carryback. § 1212(c) lets net 1256 losses carry back 3 years
  against prior 1256 gains; tracking that state is a separate
  module (analogous to ``carryover.py``).
- Mixed straddles (§ 1256(d) election to opt out of MTM for
  hedging-pair offsets).
- Dealer equity options held by a *non-dealer* — out of scope.
- Form 6781 Part II (gains/losses from straddles) and Part III
  (unrecognized losses on positions held at year-end). Only Part I
  (Section 1256 60/40 split) is implemented here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# 60 % long-term + 40 % short-term split per § 1256(a)(3).
LONG_TERM_PCT: Decimal = Decimal("0.60")
SHORT_TERM_PCT: Decimal = Decimal("0.40")


@dataclass(frozen=True)
class Section1256Contract:
    """One Section 1256 contract entry on Form 6781 Part I.

    ``proceeds_or_fmv`` is either the actual sale proceeds (closed
    contract) or the year-end fair-market value (MTM open contract).
    ``cost`` is the basis: original cost for closed contracts, the
    prior-year MTM value for contracts held over a year-end. Both
    are non-negative.
    """

    description: str
    acquired: date
    closed_or_year_end: date
    proceeds_or_fmv: Decimal
    cost: Decimal

    def __post_init__(self) -> None:
        if self.acquired > self.closed_or_year_end:
            raise ValueError(
                f"acquired {self.acquired} is after "
                f"closed_or_year_end {self.closed_or_year_end}"
            )
        if self.proceeds_or_fmv < 0:
            raise ValueError("proceeds_or_fmv must be non-negative")
        if self.cost < 0:
            raise ValueError("cost must be non-negative")

    @property
    def gain_loss(self) -> Decimal:
        return (self.proceeds_or_fmv - self.cost).quantize(_TWOPLACES)


@dataclass(frozen=True)
class Form6781Summary:
    """Form 6781 Part I aggregate totals (USD).

    The 60/40 split is applied to ``net_gain_loss`` whether positive
    or negative; ``short_term_amount`` and ``long_term_amount`` keep
    the original sign so a net loss splits into two negative pieces.
    """

    contract_count: int
    proceeds_or_fmv_total: Decimal
    cost_total: Decimal
    net_gain_loss: Decimal
    short_term_amount: Decimal  # 40 % of net_gain_loss
    long_term_amount: Decimal  # 60 % of net_gain_loss


def summarize_form6781(
    contracts: list[Section1256Contract],
) -> Form6781Summary:
    """Aggregate ``contracts`` into Form 6781 Part I totals.

    The 60/40 split is mandatory regardless of holding period — the
    function does *not* inspect ``acquired`` / ``closed_or_year_end``
    other than for validation. Operators who need actual-holding
    classification use the standard 1099-B path instead.
    """
    proceeds_total = _ZERO
    cost_total = _ZERO
    net = _ZERO
    for c in contracts:
        proceeds_total += c.proceeds_or_fmv
        cost_total += c.cost
        net += c.gain_loss

    net = net.quantize(_TWOPLACES)
    short = (net * SHORT_TERM_PCT).quantize(_TWOPLACES)
    # Compute long as net - short so the two halves always add back to
    # the total even with rounding (the IRS form does the same).
    long_term = (net - short).quantize(_TWOPLACES)

    return Form6781Summary(
        contract_count=len(contracts),
        proceeds_or_fmv_total=proceeds_total.quantize(_TWOPLACES),
        cost_total=cost_total.quantize(_TWOPLACES),
        net_gain_loss=net,
        short_term_amount=short,
        long_term_amount=long_term,
    )


def contracts_to_csv(contracts: list[Section1256Contract]) -> str:
    """Render the per-contract entries in a Form-6781-shaped CSV.

    Columns mirror the form's Part I layout (description, dates,
    proceeds-or-FMV, cost basis, gain/loss). Dates are ISO 8601;
    money is quantised to two decimals.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "description",
            "acquired",
            "closed_or_year_end",
            "proceeds_or_fmv",
            "cost",
            "gain_loss",
        ]
    )
    for c in contracts:
        writer.writerow(
            [
                c.description,
                c.acquired.isoformat(),
                c.closed_or_year_end.isoformat(),
                _fmt(c.proceeds_or_fmv),
                _fmt(c.cost),
                _fmt(c.gain_loss),
            ]
        )
    return buf.getvalue()


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(_TWOPLACES)}"


__all__ = [
    "LONG_TERM_PCT",
    "SHORT_TERM_PCT",
    "Form6781Summary",
    "Section1256Contract",
    "contracts_to_csv",
    "summarize_form6781",
]
