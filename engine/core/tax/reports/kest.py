"""German Kapitalertragsteuer / Abgeltungsteuer (§ 32d EStG) summary.

Computes the year-level totals a German private investor needs to
file: gross capital gains, Sparer-Pauschbetrag (saver's lump-sum
allowance), KESt (25 % flat capital-gains tax), Solidaritätszuschlag
(5.5 % surcharge on the KESt), and optionally Kirchensteuer (church
tax — 8 % in Bayern/Baden-Württemberg, 9 % elsewhere).

Filing model
------------
The Abgeltungsteuer regime nets gains against losses *within asset
buckets* before the allowance is applied. The most important real
distinction is between equity (Aktien) losses, which by law can only
offset equity gains, and "other" capital gains. Operators tag each
disposal with an :class:`AssetClass`; the summariser nets within each
bucket, sums the resulting net gains, applies the allowance, and
computes the tax.

What's NOT here (explicit follow-ups)
-------------------------------------
- Loss carry-forward (``Verlustvortrag``). Equity losses that exceed
  equity gains carry forward indefinitely; tracking that state lives
  in a separate carryover module (deferred — analogous to the US
  ``carryover.py``).
- Teilfreistellung for fund products (Investmentsteuergesetz exemption
  rates 15 % / 30 % / 60 % / 80 %). The caller is expected to apply
  the relevant exemption upstream and pass the post-exemption gain.
- Quellensteuer-Anrechnung (foreign withholding tax credits).
- Section 6 InvStG mark-to-market on funds (Vorabpauschale).
- NV-Bescheinigung / Freistellungsauftrag handling at the broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# § 32d EStG flat tax + § 4 SolzG.
KEST_RATE: Decimal = Decimal("0.25")
SOLZ_RATE: Decimal = Decimal("0.055")  # applied on the KESt amount

# Sparer-Pauschbetrag — annual allowance for capital income.
SPARER_PAUSCHBETRAG_2023: Decimal = Decimal("1000.00")
SPARER_PAUSCHBETRAG_JOINT_2023: Decimal = Decimal("2000.00")
# 2024+ left unchanged at 1000/2000; constants exposed by year so
# callers can be explicit.
SPARER_PAUSCHBETRAG_2024: Decimal = SPARER_PAUSCHBETRAG_2023
SPARER_PAUSCHBETRAG_JOINT_2024: Decimal = SPARER_PAUSCHBETRAG_JOINT_2023

# Kirchensteuer rates (legally fixed per Bundesland).
CHURCH_TAX_RATE_BAYERN_BW: Decimal = Decimal("0.08")
CHURCH_TAX_RATE_OTHER: Decimal = Decimal("0.09")


class AssetClass(StrEnum):
    """Coarse asset bucket. ``EQUITY`` losses are ring-fenced under
    § 20 Abs. 6 Satz 4 EStG; ``OTHER`` losses can offset any other
    capital income."""

    EQUITY = "equity"
    OTHER = "other"


@dataclass(frozen=True)
class KestDisposal:
    description: str
    acquired: date
    disposed: date
    proceeds: Decimal
    cost: Decimal
    asset_class: AssetClass = AssetClass.EQUITY

    def __post_init__(self) -> None:
        if self.acquired > self.disposed:
            raise ValueError(f"acquired {self.acquired} is after disposed {self.disposed}")
        if self.proceeds < 0:
            raise ValueError("proceeds must be non-negative")
        if self.cost < 0:
            raise ValueError("cost must be non-negative")

    @property
    def gain_loss(self) -> Decimal:
        return (self.proceeds - self.cost).quantize(_TWOPLACES)


@dataclass(frozen=True)
class KestSummary:
    """Year-level KESt totals (EUR)."""

    disposal_count: int
    proceeds_total: Decimal
    cost_total: Decimal
    equity_net: Decimal  # may be negative (carry-forward eligible)
    other_net: Decimal  # may be negative
    taxable_income: Decimal  # after netting + allowance
    allowance_used: Decimal
    kest: Decimal
    solidarity_surcharge: Decimal
    church_tax: Decimal
    total_tax: Decimal


def summarize_kest(
    disposals: list[KestDisposal],
    *,
    allowance: Decimal = SPARER_PAUSCHBETRAG_2024,
    church_tax_rate: Decimal | None = None,
) -> KestSummary:
    """Aggregate ``disposals`` into a year-level KESt summary.

    ``allowance`` defaults to the 2024 single-filer Sparer-Pauschbetrag
    (€1,000); jointly-assessed couples pass
    :data:`SPARER_PAUSCHBETRAG_JOINT_2024`. ``church_tax_rate`` is
    optional; pass :data:`CHURCH_TAX_RATE_BAYERN_BW` (8 %) or
    :data:`CHURCH_TAX_RATE_OTHER` (9 %) to include Kirchensteuer.

    Equity losses are ring-fenced (§ 20 Abs. 6 Satz 4 EStG): a negative
    equity bucket does *not* reduce a positive ``other`` bucket. The
    summary surfaces both per-bucket nets so the operator can carry an
    equity-only loss forward independently.
    """
    if allowance < 0:
        raise ValueError("allowance must be non-negative")
    if church_tax_rate is not None and church_tax_rate < 0:
        raise ValueError("church_tax_rate must be non-negative")

    proceeds_total = _ZERO
    cost_total = _ZERO
    equity = _ZERO
    other = _ZERO
    for d in disposals:
        proceeds_total += d.proceeds
        cost_total += d.cost
        if d.asset_class == AssetClass.EQUITY:
            equity += d.gain_loss
        else:
            other += d.gain_loss

    equity_net = equity.quantize(_TWOPLACES)
    other_net = other.quantize(_TWOPLACES)

    # § 20 Abs. 6 Satz 4 EStG: only positive equity contributes to the
    # current-year base. Negative equity is set aside for carry-forward.
    taxable_equity = equity_net if equity_net > 0 else _ZERO
    # Other-bucket losses can offset any other capital income, so they
    # contribute their signed value (the caller may net against
    # interest / dividends upstream).
    base = (taxable_equity + other_net).quantize(_TWOPLACES)
    if base < 0:
        base = _ZERO

    allowance_used = min(base, allowance).quantize(_TWOPLACES)
    taxable_income = (base - allowance_used).quantize(_TWOPLACES)

    kest = (taxable_income * KEST_RATE).quantize(_TWOPLACES)
    solz = (kest * SOLZ_RATE).quantize(_TWOPLACES)
    church_tax = _ZERO
    if church_tax_rate is not None:
        church_tax = (kest * church_tax_rate).quantize(_TWOPLACES)
    total_tax = (kest + solz + church_tax).quantize(_TWOPLACES)

    return KestSummary(
        disposal_count=len(disposals),
        proceeds_total=proceeds_total.quantize(_TWOPLACES),
        cost_total=cost_total.quantize(_TWOPLACES),
        equity_net=equity_net,
        other_net=other_net,
        taxable_income=taxable_income,
        allowance_used=allowance_used,
        kest=kest,
        solidarity_surcharge=solz,
        church_tax=church_tax,
        total_tax=total_tax,
    )


__all__ = [
    "CHURCH_TAX_RATE_BAYERN_BW",
    "CHURCH_TAX_RATE_OTHER",
    "KEST_RATE",
    "SOLZ_RATE",
    "SPARER_PAUSCHBETRAG_2023",
    "SPARER_PAUSCHBETRAG_2024",
    "SPARER_PAUSCHBETRAG_JOINT_2023",
    "SPARER_PAUSCHBETRAG_JOINT_2024",
    "AssetClass",
    "KestDisposal",
    "KestSummary",
    "summarize_kest",
]
