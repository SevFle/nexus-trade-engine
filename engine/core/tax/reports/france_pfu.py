"""France Prélèvement Forfaitaire Unique (PFU / flat tax) summary.

Computes the year-level totals a French resident files for capital
gains on stocks and similar securities under the PFU regime introduced
by the Loi de finances pour 2018 (CGI Article 200 A). The flat 30 %
total breaks down as:

- 12.8 % impôt sur le revenu (income-tax fraction).
- 17.2 % prélèvements sociaux (CSG / CRDS / contribution de
  solidarité — collectively "social levies").

What's NOT here (explicit follow-ups)
-------------------------------------
- *Barème progressif* election. The taxpayer may opt to apply the
  progressive income-tax scale instead of the flat 12.8 % component
  (CGI Art. 200 A 2). The election is global and separate from the
  social-levy component. Operators who need that path build it on
  top of this summary's pre-tax ``net_gain``.
- *Capital-loss carry-forward*. Net losses on stock disposals carry
  forward 10 years against future stock gains (CGI Art. 150-0 D 11).
  Tracking that state belongs in a separate carryover module
  (analogous to the US ``carryover.py``) and is deferred.
- *Abattement pour durée de détention*. Available under the legacy
  pre-PFU regime (CGI Art. 150-0 D 1 quater) only when the barème
  progressif is elected on shares acquired before 2018 — out of
  scope for the default flat-tax path here.
- *Plus-values immobilières* (real-estate gains under CGI Art. 150 U)
  — different schema and rates entirely.
- *PEA-eligible holdings*. PEAs have their own exemption schedule
  (CGI Art. 157 5° bis) — the caller is expected to filter those
  before passing disposals to this summariser.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# Loi de finances pour 2018 — Article 200 A CGI.
PFU_INCOME_TAX_RATE: Decimal = Decimal("0.128")
PFU_SOCIAL_CHARGES_RATE: Decimal = Decimal("0.172")
# Sanity check — the two components must add to the famous 30 %.
PFU_TOTAL_RATE: Decimal = PFU_INCOME_TAX_RATE + PFU_SOCIAL_CHARGES_RATE


@dataclass(frozen=True)
class PfuDisposal:
    """One taxable disposal under the PFU regime.

    The PFU is symbol-agnostic at this layer — operators may pre-filter
    PEA-eligible holdings or non-PFU instruments upstream.
    """

    description: str
    acquired: date
    disposed: date
    proceeds: Decimal
    cost: Decimal

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
class PfuSummary:
    """Year-level PFU totals (EUR)."""

    disposal_count: int
    proceeds_total: Decimal
    cost_total: Decimal
    net_gain: Decimal
    net_loss: Decimal
    taxable_gain: Decimal  # equals net_gain (no allowance under PFU)
    income_tax: Decimal  # 12.8 % component
    social_charges: Decimal  # 17.2 % component
    total_tax: Decimal


def summarize_pfu(disposals: list[PfuDisposal]) -> PfuSummary:
    """Aggregate ``disposals`` into a year-level PFU summary.

    Loss years emit zero tax: net losses are not refundable and the
    PFU regime has no annual allowance. Operators are expected to
    track the net-loss bucket separately for the 10-year carry-forward
    against future stock gains.
    """
    proceeds_total = _ZERO
    cost_total = _ZERO
    gross_gain = _ZERO
    gross_loss = _ZERO
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

    income_tax = (net_gain * PFU_INCOME_TAX_RATE).quantize(_TWOPLACES)
    social_charges = (net_gain * PFU_SOCIAL_CHARGES_RATE).quantize(_TWOPLACES)
    total_tax = (income_tax + social_charges).quantize(_TWOPLACES)

    return PfuSummary(
        disposal_count=len(disposals),
        proceeds_total=proceeds_total.quantize(_TWOPLACES),
        cost_total=cost_total.quantize(_TWOPLACES),
        net_gain=net_gain,
        net_loss=net_loss,
        taxable_gain=net_gain,
        income_tax=income_tax,
        social_charges=social_charges,
        total_tax=total_tax,
    )


__all__ = [
    "PFU_INCOME_TAX_RATE",
    "PFU_SOCIAL_CHARGES_RATE",
    "PFU_TOTAL_RATE",
    "PfuDisposal",
    "PfuSummary",
    "summarize_pfu",
]
