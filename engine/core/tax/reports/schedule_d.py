"""US Schedule D (Form 1040) summary aggregator (gh#155).

The IRS Schedule D rolls up the per-lot rows on Form 8949 into two
totals:

- Part I — Short-Term Capital Gains and Losses (lots held ≤ 1 year).
- Part II — Long-Term Capital Gains and Losses (lots held > 1 year).

Each part has its own (proceeds, cost_basis, adjustment, gain_loss)
totals; the net of the two becomes the entry on Form 1040, Schedule 1,
line 7. This module computes those totals from the per-lot
:class:`Schedule1099BRow` records produced by
:func:`engine.core.tax.reports.form_1099b.generate_1099b_rows`.

Out of scope (explicit follow-ups):
- Capital-loss carryover between tax years.
- Section 1256 contracts (Form 6781) — different schema entirely.
- Per-jurisdiction summaries (HMRC CGT, KESt, MiFID II) — they
  belong in their own modules under this package.
- AMT (alternative minimum tax) basis differences.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal

from engine.core.tax.reports.form_1099b import HoldingTerm, Schedule1099BRow

_TWOPLACES = Decimal("0.01")


@dataclass(frozen=True)
class ScheduleDPartTotal:
    """Aggregate totals for a single Schedule D part."""

    row_count: int
    proceeds: Decimal
    cost_basis: Decimal
    adjustment_amount: Decimal
    gain_loss: Decimal


@dataclass(frozen=True)
class ScheduleDSummary:
    """Both parts plus the net result.

    ``net_gain_loss`` equals ``short_term.gain_loss + long_term.gain_loss``.
    """

    short_term: ScheduleDPartTotal
    long_term: ScheduleDPartTotal
    net_gain_loss: Decimal


def summarize_schedule_d(rows: list[Schedule1099BRow]) -> ScheduleDSummary:
    """Aggregate ``rows`` into Schedule D Part I + Part II totals.

    Money columns are quantised to two decimal places — Schedule D is
    expressed in whole dollars on the IRS form, but engines typically
    keep cents through the pipeline so the numbers round-trip cleanly
    against per-fill audits.
    """
    short = _empty_part()
    long_ = _empty_part()
    for row in rows:
        bucket = short if row.term == HoldingTerm.SHORT_TERM else long_
        bucket["row_count"] += 1
        bucket["proceeds"] += row.proceeds
        bucket["cost_basis"] += row.cost_basis
        bucket["adjustment_amount"] += row.adjustment_amount
        bucket["gain_loss"] += row.gain_loss

    short_total = _materialise(short)
    long_total = _materialise(long_)
    net = (short_total.gain_loss + long_total.gain_loss).quantize(_TWOPLACES)
    return ScheduleDSummary(
        short_term=short_total,
        long_term=long_total,
        net_gain_loss=net,
    )


def summary_to_csv(summary: ScheduleDSummary) -> str:
    """Serialise the summary as a 4-line CSV (header + short + long + net).

    Columns mirror the Schedule D form line items so a CPA can paste
    the result straight into the IRS PDF or a tax-prep tool that
    accepts CSV imports.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "section",
            "row_count",
            "proceeds",
            "cost_basis",
            "adjustment_amount",
            "gain_loss",
        ]
    )
    for label, part in (
        ("part_i_short_term", summary.short_term),
        ("part_ii_long_term", summary.long_term),
    ):
        writer.writerow(
            [
                label,
                part.row_count,
                _fmt(part.proceeds),
                _fmt(part.cost_basis),
                _fmt(part.adjustment_amount),
                _fmt(part.gain_loss),
            ]
        )
    writer.writerow(
        [
            "net",
            summary.short_term.row_count + summary.long_term.row_count,
            "",
            "",
            "",
            _fmt(summary.net_gain_loss),
        ]
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _empty_part() -> dict[str, Decimal | int]:
    return {
        "row_count": 0,
        "proceeds": Decimal("0"),
        "cost_basis": Decimal("0"),
        "adjustment_amount": Decimal("0"),
        "gain_loss": Decimal("0"),
    }


def _materialise(part: dict) -> ScheduleDPartTotal:
    return ScheduleDPartTotal(
        row_count=part["row_count"],
        proceeds=part["proceeds"].quantize(_TWOPLACES),
        cost_basis=part["cost_basis"].quantize(_TWOPLACES),
        adjustment_amount=part["adjustment_amount"].quantize(_TWOPLACES),
        gain_loss=part["gain_loss"].quantize(_TWOPLACES),
    )


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(_TWOPLACES)}"


__all__ = [
    "ScheduleDPartTotal",
    "ScheduleDSummary",
    "summarize_schedule_d",
    "summary_to_csv",
]
