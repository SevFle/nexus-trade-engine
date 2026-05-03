"""IRS Form 6781 Part II — Section 1092 straddle-loss limitation.

A straddle (IRC § 1092) is an offsetting pair of positions in actively-
traded personal property: closing one leg at a loss when the other
leg still has an unrealized gain triggers a *deferral* of the loss to
the extent of the offsetting unrealized gain.

Per-leg rule (Form 6781 Part II)
-------------------------------
For each straddle pair where the loss leg has been closed:

- ``allowed_loss = max(recognized_loss - unrecognized_offsetting_gain, 0)``
- ``deferred_loss = recognized_loss - allowed_loss``

The deferred portion carries forward into the basis of the still-open
offsetting leg under § 1092(a)(1)(B). This module does not modify the
basis itself — it surfaces the ``deferred_loss`` so the caller's lot
ledger applies the adjustment.

What's NOT here (explicit follow-ups)
-------------------------------------
- Identification of straddles. The caller pairs the legs upstream;
  detection logic (covered calls, married puts, debit spreads, etc.)
  is a separate concern.
- Mixed-straddle § 1256(d) opt-out election. When elected, a mixed
  straddle is reported on Form 6781 Part I instead — Part II is for
  non-elected straddles.
- Form 6781 Part III (unrecognized year-end positions disclosure).
- § 263(g) carrying-charge capitalization on straddles.
- Identified-Straddle election under § 1092(a)(2) (cross-pair offset).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class StraddleLeg:
    """One closed loss leg with the offsetting leg's unrecognized gain.

    Both ``recognized_loss`` and ``unrecognized_offsetting_gain`` are
    *non-negative* magnitudes (not signed gain/loss values). The leg
    represents a closed position that booked a loss; the offset is the
    still-open opposing position's unrealized gain at the disposal
    date.
    """

    description: str
    recognized_loss: Decimal
    unrecognized_offsetting_gain: Decimal

    def __post_init__(self) -> None:
        if self.recognized_loss < 0:
            raise ValueError("recognized_loss must be non-negative")
        if self.unrecognized_offsetting_gain < 0:
            raise ValueError(
                "unrecognized_offsetting_gain must be non-negative"
            )

    @property
    def allowed_loss(self) -> Decimal:
        """Per-leg allowed loss: only the portion above the offset."""
        delta = self.recognized_loss - self.unrecognized_offsetting_gain
        return delta.quantize(_TWOPLACES) if delta > 0 else _ZERO

    @property
    def deferred_loss(self) -> Decimal:
        """Per-leg deferred loss = recognized - allowed."""
        return (self.recognized_loss - self.allowed_loss).quantize(_TWOPLACES)


@dataclass(frozen=True)
class Form6781PartIISummary:
    """Year-level Form 6781 Part II totals (USD)."""

    leg_count: int
    total_recognized_loss: Decimal
    total_unrecognized_offsetting_gain: Decimal
    total_allowed_loss: Decimal
    total_deferred_loss: Decimal


def summarize_form6781_part_ii(
    legs: list[StraddleLeg],
) -> Form6781PartIISummary:
    """Aggregate ``legs`` into the Form 6781 Part II per-year totals.

    The per-leg deferral is independent (each pair stands alone) — no
    cross-pair offset under § 1092 outside an Identified-Straddle
    election (which we don't model). The summary therefore sums each
    leg's own allowed and deferred amounts.
    """
    total_loss = _ZERO
    total_offset = _ZERO
    total_allowed = _ZERO
    total_deferred = _ZERO
    for leg in legs:
        total_loss += leg.recognized_loss
        total_offset += leg.unrecognized_offsetting_gain
        total_allowed += leg.allowed_loss
        total_deferred += leg.deferred_loss

    return Form6781PartIISummary(
        leg_count=len(legs),
        total_recognized_loss=total_loss.quantize(_TWOPLACES),
        total_unrecognized_offsetting_gain=total_offset.quantize(_TWOPLACES),
        total_allowed_loss=total_allowed.quantize(_TWOPLACES),
        total_deferred_loss=total_deferred.quantize(_TWOPLACES),
    )


def legs_to_csv(legs: list[StraddleLeg]) -> str:
    """Render the per-leg detail in a Form-6781-Part-II-shaped CSV.

    Columns mirror the form's Part II layout (description + the four
    per-leg amounts). Money is quantised to two decimals.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "description",
            "recognized_loss",
            "unrecognized_offsetting_gain",
            "allowed_loss",
            "deferred_loss",
        ]
    )
    for leg in legs:
        writer.writerow(
            [
                leg.description,
                _fmt(leg.recognized_loss),
                _fmt(leg.unrecognized_offsetting_gain),
                _fmt(leg.allowed_loss),
                _fmt(leg.deferred_loss),
            ]
        )
    return buf.getvalue()


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(_TWOPLACES)}"


__all__ = [
    "Form6781PartIISummary",
    "StraddleLeg",
    "legs_to_csv",
    "summarize_form6781_part_ii",
]
