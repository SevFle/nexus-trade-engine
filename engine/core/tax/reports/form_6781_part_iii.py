"""IRS Form 6781 Part III — Unrecognized Gains on Year-End Positions.

Form 6781 Part III is *disclosure-only*: a taxpayer with positions
still open on the last day of the tax year where the fair-market
value exceeds basis must list each such position. There is no tax
computation here — Part I (Section 1256 60/40 split) and Part II
(§ 1092 straddle-loss deferral) do that work.

Per-position rule
-----------------
A position appears on Part III when it has an *unrecognized gain* at
year-end:

    unrecognized_gain = max(year_end_fmv - basis, 0)

Positions that are at-the-money or under water (``fmv <= basis``)
do not appear. The summary surfaces the count and the aggregate
unrecognized-gain total — useful as a sanity check against the
caller's lot ledger.

What's NOT here (explicit follow-ups)
-------------------------------------
- Holding-period classification (Part III is reported regardless of
  holding period — the IRS uses it for aggregate disclosure).
- Identification of which positions are part of a straddle pair
  (the caller decides).
- The Form 6781 attached statement listing each position with the
  full set of optional metadata (CUSIP, account, broker LEI, etc.).
- Schedule D pass-through. Part III is informational only.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class YearEndPosition:
    """One open position at year-end with its basis + year-end FMV.

    Both money fields are non-negative. Positions where
    ``year_end_fmv <= basis`` produce a zero ``unrecognized_gain`` and
    are filtered out of the summary by
    :func:`summarize_form6781_part_iii`.
    """

    description: str
    acquired: date
    year_end: date
    basis: Decimal
    year_end_fmv: Decimal

    def __post_init__(self) -> None:
        if self.acquired > self.year_end:
            raise ValueError(f"acquired {self.acquired} is after year_end {self.year_end}")
        if self.basis < 0:
            raise ValueError("basis must be non-negative")
        if self.year_end_fmv < 0:
            raise ValueError("year_end_fmv must be non-negative")

    @property
    def unrecognized_gain(self) -> Decimal:
        delta = self.year_end_fmv - self.basis
        return delta.quantize(_TWOPLACES) if delta > 0 else _ZERO

    @property
    def has_unrecognized_gain(self) -> bool:
        return self.unrecognized_gain > 0


@dataclass(frozen=True)
class Form6781PartIIISummary:
    """Year-level Form 6781 Part III totals (USD).

    - ``position_count`` — the number of positions actually reported
      (i.e. those with a positive ``unrecognized_gain``).
    - ``total_unrecognized_gain`` — sum of the per-position
      unrecognized gains.
    - ``positions`` — the filtered list, oldest-acquisition-first so
      callers can directly serialise into the form's attached
      statement.
    """

    position_count: int
    total_unrecognized_gain: Decimal
    positions: tuple[YearEndPosition, ...]


def summarize_form6781_part_iii(
    positions: list[YearEndPosition],
) -> Form6781PartIIISummary:
    """Filter ``positions`` to those with a positive unrecognized gain
    and aggregate the totals. Positions are sorted by acquisition date
    (oldest first) for stable output.
    """
    reportable = [p for p in positions if p.has_unrecognized_gain]
    reportable.sort(key=lambda p: p.acquired)

    total = _ZERO
    for p in reportable:
        total += p.unrecognized_gain

    return Form6781PartIIISummary(
        position_count=len(reportable),
        total_unrecognized_gain=total.quantize(_TWOPLACES),
        positions=tuple(reportable),
    )


def positions_to_csv(positions: list[YearEndPosition]) -> str:
    """Render the per-position detail in a Form-6781-Part-III-shaped
    CSV. Includes every input position (including under-water ones,
    so the caller can audit the filter); ``unrecognized_gain`` is the
    derived column the IRS form actually displays.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "description",
            "acquired",
            "year_end",
            "basis",
            "year_end_fmv",
            "unrecognized_gain",
        ]
    )
    for p in positions:
        writer.writerow(
            [
                p.description,
                p.acquired.isoformat(),
                p.year_end.isoformat(),
                _fmt(p.basis),
                _fmt(p.year_end_fmv),
                _fmt(p.unrecognized_gain),
            ]
        )
    return buf.getvalue()


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(_TWOPLACES)}"


__all__ = [
    "Form6781PartIIISummary",
    "YearEndPosition",
    "positions_to_csv",
    "summarize_form6781_part_iii",
]
