"""IRS Form 1099-B / Schedule D / Form 8949 row generator (gh#155).

This module turns a stream of *lot dispositions* (a buy lot that was
fully or partially sold) into the per-line records the IRS expects on
Form 1099-B Box 1d/1e/1f and Schedule D / Form 8949.

What's covered
--------------
- Per-lot rows with description, dates, proceeds, basis, gain/loss.
- Long-term vs short-term classification (>1 year held).
- Wash-sale "W" code + adjustment column (column g on Form 8949).
- CSV serialisation with the column names brokers use on their
  consolidated 1099-B exports (so a CPA can paste this into existing
  workflows).

What's NOT covered (explicit follow-ups)
----------------------------------------
- Section 1256 contracts (Form 6781) — different schema entirely.
- Crypto-specific reporting (Form 8949 with the "C" basis code is
  fine here; specialised wallet-by-wallet reports are not).
- Wash-sale across accounts. The detector at
  ``engine.core.tax.wash_sale`` deliberately operates on a
  caller-provided trade scope; this generator inherits that scope.
- Box 1g cost-basis reporting reconciliation against broker 1099-B.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

# Boundary between short-term and long-term capital gains under
# 26 U.S.C. § 1222: more than one year held = long-term.
_LONG_TERM_DAYS: int = 365


class HoldingTerm(str, Enum):
    SHORT_TERM = "short"
    LONG_TERM = "long"


@dataclass(frozen=True)
class LotDisposition:
    """One lot (or fraction of one) that was sold.

    The caller is responsible for splitting partial sells into
    multiple ``LotDisposition`` records — each row on Form 8949 is a
    single lot disposition. ``wash_sale_disallowed`` should be the
    amount surfaced by ``engine.core.tax.wash_sale.detect_wash_sales``
    for this disposition (or zero if there is none).
    """

    description: str  # e.g. "10.000 shares AAPL"
    acquired: date
    sold: date
    proceeds: Decimal  # gross sale proceeds (column 1d)
    cost_basis: Decimal  # adjusted cost basis (column 1e)
    wash_sale_disallowed: Decimal = Decimal("0")
    # Per-trade label that survives back to the caller. Useful for
    # reconciling a row with the underlying fill / lot.
    lot_id: str | None = None

    def __post_init__(self) -> None:
        if self.acquired > self.sold:
            raise ValueError(
                f"acquired {self.acquired} is after sold {self.sold}"
            )
        if self.proceeds < 0:
            raise ValueError("proceeds must be non-negative")
        if self.cost_basis < 0:
            raise ValueError("cost_basis must be non-negative")
        if self.wash_sale_disallowed < 0:
            raise ValueError("wash_sale_disallowed must be non-negative")


@dataclass(frozen=True)
class Schedule1099BRow:
    """Materialised Form 8949 row.

    Columns mirror the IRS form:

    - ``description`` (a)
    - ``acquired`` (b)
    - ``sold`` (c)
    - ``proceeds`` (d)
    - ``cost_basis`` (e)
    - ``adjustment_codes`` (f)
    - ``adjustment_amount`` (g)
    - ``gain_loss`` (h) — calculated as d - e + g (g is *positive*
      when it represents a disallowed loss being added back).
    - ``term`` — short-term / long-term, picks Form 8949 Part I vs II.
    - ``lot_id`` — reconciliation hint, not on the form.
    """

    description: str
    acquired: date
    sold: date
    proceeds: Decimal
    cost_basis: Decimal
    adjustment_codes: str
    adjustment_amount: Decimal
    gain_loss: Decimal
    term: HoldingTerm
    lot_id: str | None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_1099b_rows(
    dispositions: list[LotDisposition],
) -> list[Schedule1099BRow]:
    """Produce one Form 8949 row per disposition. Order is preserved."""
    rows: list[Schedule1099BRow] = []
    for d in dispositions:
        term = _holding_term(d.acquired, d.sold)
        codes_parts: list[str] = []
        adjustment = Decimal("0")
        if d.wash_sale_disallowed > 0:
            codes_parts.append("W")
            # Wash-sale adjustment: positive = disallowed loss added back.
            adjustment += d.wash_sale_disallowed
        codes = "".join(codes_parts)
        gain_loss = (d.proceeds - d.cost_basis + adjustment).quantize(
            Decimal("0.01")
        )
        rows.append(
            Schedule1099BRow(
                description=d.description,
                acquired=d.acquired,
                sold=d.sold,
                proceeds=d.proceeds,
                cost_basis=d.cost_basis,
                adjustment_codes=codes,
                adjustment_amount=adjustment,
                gain_loss=gain_loss,
                term=term,
                lot_id=d.lot_id,
            )
        )
    return rows


def rows_to_csv(rows: list[Schedule1099BRow]) -> str:
    """Serialise rows to a Form-8949-shaped CSV.

    Header columns match the form letter codes (a/b/c/d/e/f/g/h) plus
    a ``term`` column for Part I vs II classification and ``lot_id``
    for reconciliation. Date columns are ISO 8601 (YYYY-MM-DD).
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "description",
            "acquired",
            "sold",
            "proceeds",
            "cost_basis",
            "adjustment_codes",
            "adjustment_amount",
            "gain_loss",
            "term",
            "lot_id",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.description,
                r.acquired.isoformat(),
                r.sold.isoformat(),
                _fmt(r.proceeds),
                _fmt(r.cost_basis),
                r.adjustment_codes,
                _fmt(r.adjustment_amount),
                _fmt(r.gain_loss),
                r.term.value,
                r.lot_id or "",
            ]
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _holding_term(acquired: date, sold: date) -> HoldingTerm:
    delta = (sold - acquired).days
    return HoldingTerm.LONG_TERM if delta > _LONG_TERM_DAYS else HoldingTerm.SHORT_TERM


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


# Coercion helper used by callers porting from the live ``datetime``
# columns on ``TaxLotRecord`` (which are ``datetime``, not ``date``).
def to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"to_date: expected date|datetime, got {type(value).__name__}")
