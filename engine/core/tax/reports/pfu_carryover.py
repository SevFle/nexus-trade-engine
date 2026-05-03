"""France PFU loss carry-forward (CGI Article 150-0 D 11).

Companion to :mod:`engine.core.tax.reports.france_pfu`. Net capital
losses on stock disposals carry forward for *ten years* and may be
used to offset future stock gains. After ten years, the loss expires.

Vintage tracking
----------------
French law dates losses by their tax year of origin: a 2024 loss is
usable in 2024–2034 inclusive, then expires. The carryover record
therefore tracks loss *vintages* (year + amount), not a single
aggregate balance like the US/UK forms. When a future year nets a
gain, the *oldest* unexpired vintage absorbs first (FIFO) — that
matches how every French tax-prep tool models the box-2DC ordering.

Algorithm per year (tax year ``Y``)
-----------------------------------
1. Net the year's disposals via :func:`summarize_pfu`.
2. Drop any vintage with ``Y - vintage.year >= 10`` — these have
   expired (10-year window).
3. If the year netted a *gain*: walk vintages oldest-first and
   absorb up to the gain. Surviving vintages stay; the gain net of
   absorbed losses becomes the taxable base; PFU is recomputed.
4. If the year netted a *loss*: append it as a new vintage tagged
   with ``Y``.

Out of scope (explicit follow-ups)
----------------------------------
- *Barème progressif* election. The taxpayer may opt for the
  progressive scale (CGI Art. 200 A 2); loss carry-forward mechanics
  are identical, but rate computation differs — out of scope here.
- Loss segmentation by asset type (PEA, dérivés / SOFICA, etc.).
  Filter upstream.
- *Plus-values immobilières* — different regime entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from engine.core.tax.reports.france_pfu import (
    PFU_INCOME_TAX_RATE,
    PFU_SOCIAL_CHARGES_RATE,
    PfuDisposal,
    PfuSummary,
    summarize_pfu,
)

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

CARRY_FORWARD_YEARS: int = 10


@dataclass(frozen=True)
class PfuLossVintage:
    """One vintage of carry-forward loss, dated by its origin year."""

    year: int
    amount: Decimal  # positive loss amount

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("vintage amount must be non-negative")


@dataclass(frozen=True)
class PfuCarryover:
    """Multi-vintage loss carryover record. ``vintages`` is sorted
    oldest-first (caller is responsible — :func:`normalised` does it
    for you)."""

    vintages: tuple[PfuLossVintage, ...] = ()

    @classmethod
    def zero(cls) -> PfuCarryover:
        return cls(vintages=())

    @property
    def total(self) -> Decimal:
        return sum(
            (v.amount for v in self.vintages), _ZERO
        ).quantize(_TWOPLACES)


def normalised(carryover: PfuCarryover) -> PfuCarryover:
    """Return a carryover with vintages sorted oldest-first and any
    zero-amount vintages dropped."""
    sorted_vs = tuple(
        sorted(
            (v for v in carryover.vintages if v.amount > 0),
            key=lambda v: v.year,
        )
    )
    return PfuCarryover(vintages=sorted_vs)


@dataclass(frozen=True)
class PfuApplication:
    """Result of running :func:`apply_pfu_carryover` for one tax year.

    - ``summary`` — base :class:`PfuSummary` for ``disposals`` (no
      carry consumption).
    - ``loss_used`` — total prior loss amount absorbed against the
      year's net gain.
    - ``taxable_gain_after_carryover`` — the year's taxable gain net
      of the absorbed prior loss.
    - ``income_tax_after_carryover`` and
      ``social_charges_after_carryover`` — recomputed at the PFU
      breakdown rates (12.8 % + 17.2 %) on the post-carryover base.
    - ``total_tax_after_carryover`` — sum of the two.
    - ``next_year_carryover`` — vintages that survive into the next
      tax year. Expired vintages are already dropped.
    - ``expired`` — vintages dropped this year because they hit the
      10-year wall. Useful for audit / UI display.
    """

    summary: PfuSummary
    loss_used: Decimal
    taxable_gain_after_carryover: Decimal
    income_tax_after_carryover: Decimal
    social_charges_after_carryover: Decimal
    total_tax_after_carryover: Decimal
    next_year_carryover: PfuCarryover
    expired: tuple[PfuLossVintage, ...]


def apply_pfu_carryover(
    disposals: list[PfuDisposal],
    prior: PfuCarryover | None = None,
    *,
    current_year: int,
) -> PfuApplication:
    """Apply ``prior`` losses (FIFO by vintage) to ``disposals``'s net
    gain, then return the new tax basis and the surviving carryover.
    """
    prior = normalised(prior or PfuCarryover.zero())
    summary = summarize_pfu(disposals)

    # Drop vintages older than the 10-year window.
    fresh_vintages: list[PfuLossVintage] = []
    expired_vintages: list[PfuLossVintage] = []
    for v in prior.vintages:
        if current_year - v.year >= CARRY_FORWARD_YEARS:
            expired_vintages.append(v)
        else:
            fresh_vintages.append(v)

    loss_used = _ZERO
    surviving = list(fresh_vintages)
    if summary.net_gain > 0:
        # Absorb oldest-first, FIFO by vintage year.
        remaining_gain = summary.net_gain
        new_surviving: list[PfuLossVintage] = []
        for v in surviving:
            if remaining_gain <= 0:
                new_surviving.append(v)
                continue
            take = min(remaining_gain, v.amount).quantize(_TWOPLACES)
            loss_used += take
            remaining_after = (v.amount - take).quantize(_TWOPLACES)
            if remaining_after > 0:
                new_surviving.append(
                    PfuLossVintage(year=v.year, amount=remaining_after)
                )
            remaining_gain = (remaining_gain - take).quantize(_TWOPLACES)
        surviving = new_surviving
    elif summary.net_loss > 0:
        # Tag the year's loss as a new vintage.
        surviving.append(
            PfuLossVintage(
                year=current_year, amount=summary.net_loss
            )
        )

    taxable_after = (summary.net_gain - loss_used).quantize(_TWOPLACES)
    income_tax = (taxable_after * PFU_INCOME_TAX_RATE).quantize(_TWOPLACES)
    social = (taxable_after * PFU_SOCIAL_CHARGES_RATE).quantize(_TWOPLACES)
    total = (income_tax + social).quantize(_TWOPLACES)

    return PfuApplication(
        summary=summary,
        loss_used=loss_used.quantize(_TWOPLACES),
        taxable_gain_after_carryover=taxable_after,
        income_tax_after_carryover=income_tax,
        social_charges_after_carryover=social,
        total_tax_after_carryover=total,
        next_year_carryover=normalised(
            PfuCarryover(vintages=tuple(surviving))
        ),
        expired=tuple(expired_vintages),
    )


__all__ = [
    "CARRY_FORWARD_YEARS",
    "PfuApplication",
    "PfuCarryover",
    "PfuLossVintage",
    "apply_pfu_carryover",
    "normalised",
]
