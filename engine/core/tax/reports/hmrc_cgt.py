"""HMRC Capital Gains Tax summary (gh#155 follow-up).

Aggregates per-disposal records into the totals HMRC expects on the
Self Assessment SA108 (Capital Gains) supplement and the online
Real-Time Transaction return.

Scope
-----
- Per-disposal proceeds, cost, gain/loss aggregation.
- Annual Exempt Amount (AEA) deduction (£3,000 for the 2024-25 tax
  year; £6,000 for 2023-24; £12,300 for 2022-23 and earlier — caller
  supplies the relevant year's allowance).
- Net taxable gain after the AEA — what feeds into the income-band
  rate calculation.

What's NOT here (explicit follow-ups):
- Section 104 share pooling — the engine's lot ledger is already
  responsible for producing per-disposal records; CGT pooling rules
  (same-day, 30-day bed-and-breakfasting, then S104 pool) are
  applied upstream by the lot accountant.
- Capital-loss carry-forward into future years (HMRC keeps loss
  registration claims; the engine should mirror that with its own
  state, deferred).
- Income-band rate selection (10%/20% basic/higher; 18%/24% on
  residential property). The summary surfaces the *taxable gain* so
  the caller's tax-band module can apply the relevant rate.
- Entrepreneur's / Investors' Relief (ER/IR) — different return.
- Non-resident capital gains for UK property.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# Annual Exempt Amount for the 2024-25 tax year. Operators handling
# prior years should pass the relevant value explicitly.
ANNUAL_EXEMPT_AMOUNT_2024_25: Decimal = Decimal("3000.00")


@dataclass(frozen=True)
class CgtDisposal:
    """One disposal: a sell of an asset that triggers CGT.

    HMRC expects per-disposal records on SA108 — one line per asset
    disposal, with proceeds, cost, and gain/loss. ``acquired`` and
    ``disposed`` capture the holding period; HMRC does not distinguish
    short vs long term but the dates are mandatory on the return.
    """

    description: str
    acquired: date
    disposed: date
    proceeds: Decimal
    cost: Decimal  # acquisition cost + allowable expenses

    def __post_init__(self) -> None:
        if self.acquired > self.disposed:
            raise ValueError(
                f"acquired {self.acquired} is after disposed {self.disposed}"
            )
        if self.proceeds < 0:
            raise ValueError("proceeds must be non-negative")
        if self.cost < 0:
            raise ValueError("cost must be non-negative")

    @property
    def gain_loss(self) -> Decimal:
        return (self.proceeds - self.cost).quantize(_TWOPLACES)


@dataclass(frozen=True)
class CgtSummary:
    """Year-level CGT totals (GBP).

    - ``proceeds_total`` and ``cost_total`` are the gross totals.
    - ``net_gain`` and ``net_loss`` are split out (HMRC reports both
      figures separately on SA108 boxes 26 and 27).
    - ``annual_exempt_amount_used`` is the portion of the AEA actually
      consumed (capped at the net gain).
    - ``taxable_gain`` is the gain after AEA — what the rate module
      multiplies by the relevant CGT rate.
    """

    disposal_count: int
    proceeds_total: Decimal
    cost_total: Decimal
    net_gain: Decimal
    net_loss: Decimal
    annual_exempt_amount_used: Decimal
    taxable_gain: Decimal


def summarize_cgt(
    disposals: list[CgtDisposal],
    *,
    annual_exempt_amount: Decimal = ANNUAL_EXEMPT_AMOUNT_2024_25,
) -> CgtSummary:
    """Aggregate ``disposals`` into a year-level CGT summary.

    ``annual_exempt_amount`` is applied only against the net gain, not
    against the gross proceeds. If gains and losses net to a loss the
    AEA is not consumed and carries no value into the loss for
    carry-forward purposes (HMRC tracks loss carry-forward separately).
    """
    if annual_exempt_amount < 0:
        raise ValueError("annual_exempt_amount must be non-negative")

    proceeds_total = _ZERO
    cost_total = _ZERO
    gross_gain = _ZERO  # sum of positive disposals
    gross_loss = _ZERO  # absolute sum of negative disposals
    for d in disposals:
        proceeds_total += d.proceeds
        cost_total += d.cost
        delta = d.gain_loss
        if delta >= 0:
            gross_gain += delta
        else:
            gross_loss += -delta

    net = (gross_gain - gross_loss).quantize(_TWOPLACES)
    if net > 0:
        net_gain = net
        net_loss = _ZERO
    else:
        net_gain = _ZERO
        net_loss = (-net).quantize(_TWOPLACES)

    aea_used = min(net_gain, annual_exempt_amount).quantize(_TWOPLACES)
    taxable_gain = (net_gain - aea_used).quantize(_TWOPLACES)

    return CgtSummary(
        disposal_count=len(disposals),
        proceeds_total=proceeds_total.quantize(_TWOPLACES),
        cost_total=cost_total.quantize(_TWOPLACES),
        net_gain=net_gain,
        net_loss=net_loss,
        annual_exempt_amount_used=aea_used,
        taxable_gain=taxable_gain,
    )


def disposals_to_csv(disposals: list[CgtDisposal]) -> str:
    """Serialise per-disposal records to a CSV that maps onto the SA108
    line items so a UK-based operator can paste straight into a
    tax-prep workflow.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "description",
            "acquired",
            "disposed",
            "proceeds",
            "cost",
            "gain_loss",
        ]
    )
    for d in disposals:
        writer.writerow(
            [
                d.description,
                d.acquired.isoformat(),
                d.disposed.isoformat(),
                _fmt(d.proceeds),
                _fmt(d.cost),
                _fmt(d.gain_loss),
            ]
        )
    return buf.getvalue()


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(_TWOPLACES)}"


__all__ = [
    "ANNUAL_EXEMPT_AMOUNT_2024_25",
    "CgtDisposal",
    "CgtSummary",
    "disposals_to_csv",
    "summarize_cgt",
]
