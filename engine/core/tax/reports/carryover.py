"""US capital-loss carryover (gh#155 follow-up).

Applies IRS § 1212(b) carryover rules to a single tax year's
Schedule D result:

1. The taxpayer first nets short-term gain/loss against long-term
   gain/loss within the same year (already done by
   :func:`engine.core.tax.reports.schedule_d.summarize_schedule_d`).
2. If the resulting net is a loss, up to ``deductible_cap`` of it can
   be deducted against ordinary income in the current year (default
   $3,000; $1,500 if Married Filing Separately).
3. The unused balance carries over to the next year, splitting back
   into short-term and long-term components — short-term losses
   absorb the deduction first, then long-term losses.

This module computes the next-year carryover and the current-year
deductible amount; it does not file the form. Operators are expected
to feed the prior-year carryover (if any) back into next year's
Schedule D run before computing the new summary.

What's NOT here (explicit follow-ups):
- Loss harvesting recommendations (different problem entirely).
- Section 1244 small-business stock loss handling.
- Joint vs. Married-Filing-Separately filing-status switches mid-year.
- Carryback (only forward carryover applies under § 1212(b) for
  individuals; § 1212(a) corporate carrybacks are out of scope).
- AMT carryover differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.core.tax.reports.schedule_d import ScheduleDSummary

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# IRS § 1211(b) annual deduction caps.
DEDUCTIBLE_CAP_DEFAULT: Decimal = Decimal("3000.00")
DEDUCTIBLE_CAP_MFS: Decimal = Decimal("1500.00")


@dataclass(frozen=True)
class CapitalLossCarryover:
    """A two-bucket carryover record. Both fields are *non-negative*
    (loss amounts) — gains do not carry over. ``zero()`` is the
    identity for the first year a taxpayer files."""

    short_term: Decimal = _ZERO
    long_term: Decimal = _ZERO

    @classmethod
    def zero(cls) -> CapitalLossCarryover:
        return cls(short_term=_ZERO, long_term=_ZERO)

    @property
    def total(self) -> Decimal:
        return (self.short_term + self.long_term).quantize(_TWOPLACES)


@dataclass(frozen=True)
class CapitalLossApplication:
    """Result of running :func:`apply_carryover` for one tax year.

    - ``current_year_deduction`` is the (positive) amount that lands on
      Form 1040 Schedule 1 line 7 as a loss against ordinary income.
      Capped at ``deductible_cap`` and at the absolute net loss.
    - ``next_year_carryover`` is what the taxpayer carries forward.
      Both legs are non-negative; either or both may be zero.
    """

    current_year_deduction: Decimal
    next_year_carryover: CapitalLossCarryover


def apply_carryover(
    summary: ScheduleDSummary,
    prior: CapitalLossCarryover | None = None,
    *,
    deductible_cap: Decimal = DEDUCTIBLE_CAP_DEFAULT,
) -> CapitalLossApplication:
    """Return the current-year deduction and next-year carryover.

    Algorithm (matches the IRS Capital Loss Carryover Worksheet):

    1. Add the prior-year carryover into the current-year per-leg
       gain/loss so prior losses get a fresh shot at offsetting any
       new gains.
    2. Net short-term against long-term as the IRS does.
    3. If the combined result is non-negative there is no loss to
       deduct or carry over; deduction = 0, next-year carryover = 0.
    4. If it is a loss, the current-year deduction is
       ``min(abs(loss), deductible_cap)``. Anything above the cap
       carries to next year.
    5. The carryover is split back into short-term first, then
       long-term, mirroring the form's Schedule D Part I / Part II
       split.
    """
    if deductible_cap <= 0:
        raise ValueError("deductible_cap must be positive")

    prior = prior or CapitalLossCarryover.zero()
    if prior.short_term < 0 or prior.long_term < 0:
        raise ValueError("prior carryover legs must be non-negative loss amounts")

    # Apply prior-year losses to this year's per-leg result.
    short_net = summary.short_term.gain_loss - prior.short_term
    long_net = summary.long_term.gain_loss - prior.long_term

    combined = (short_net + long_net).quantize(_TWOPLACES)
    if combined >= 0:
        return CapitalLossApplication(
            current_year_deduction=_ZERO,
            next_year_carryover=CapitalLossCarryover.zero(),
        )

    total_loss = -combined  # positive amount
    deduction = min(total_loss, deductible_cap).quantize(_TWOPLACES)
    remaining = (total_loss - deduction).quantize(_TWOPLACES)

    # Split the remaining loss back into short-term and long-term
    # buckets. Pull the deduction off short-term first (if it was a
    # loss), matching the IRS form's Schedule D ordering.
    short_loss = -short_net if short_net < 0 else _ZERO
    long_loss = -long_net if long_net < 0 else _ZERO

    if short_loss + long_loss == total_loss:
        # Both legs are losses (or one is zero). Apply the deduction
        # to the short-term leg first, then the long-term leg.
        next_short = max(_ZERO, short_loss - deduction).quantize(_TWOPLACES)
        absorbed_by_short = short_loss - next_short
        deduction_after_short = (deduction - absorbed_by_short).quantize(_TWOPLACES)
        next_long = max(_ZERO, long_loss - deduction_after_short).quantize(_TWOPLACES)
    else:
        # One leg is a gain that already absorbed part of the other
        # leg's loss. ``remaining`` lives entirely on whichever leg is
        # still in loss after netting.
        next_short = remaining if short_net < 0 else _ZERO
        next_long = remaining if long_net < 0 else _ZERO

    return CapitalLossApplication(
        current_year_deduction=deduction,
        next_year_carryover=CapitalLossCarryover(
            short_term=next_short.quantize(_TWOPLACES),
            long_term=next_long.quantize(_TWOPLACES),
        ),
    )


__all__ = [
    "DEDUCTIBLE_CAP_DEFAULT",
    "DEDUCTIBLE_CAP_MFS",
    "CapitalLossApplication",
    "CapitalLossCarryover",
    "apply_carryover",
]
