"""Multi-jurisdiction tax-report dispatcher (gh#155 follow-up).

A neutral :class:`TaxableDisposal` record describes a single sale event
in jurisdiction-agnostic form. :func:`report_for_jurisdiction` routes
the list of disposals to the right per-jurisdiction summariser:

- ``US`` → :class:`engine.core.tax.reports.ScheduleDSummary`
  (per-lot Form 8949 rows + Schedule D Part I/II totals).
- ``GB`` → :class:`engine.core.tax.reports.CgtSummary` (SA108).
- ``DE`` → :class:`engine.core.tax.reports.KestSummary` (§ 32d EStG).
- ``FR`` → :class:`engine.core.tax.reports.PfuSummary`
  (CGI Article 200 A flat tax).

The dispatcher is intentionally thin: each jurisdiction already owns
its own summariser; this is just a single entry point so a higher
layer (API endpoint, CLI exporter) can pick "render this taxpayer's
year" without growing per-jurisdiction switches inside its own code.

Out of scope (explicit follow-ups)
----------------------------------
- Carry-over state. Each jurisdiction's carry-over bookkeeping lives
  in its own module (only the US one is implemented today, see
  ``carryover.py``).
- ``KestDisposal.asset_class`` is hard-defaulted to ``EQUITY`` from a
  neutral disposal. Callers who need to feed in non-equity gains
  build the per-jurisdiction record set themselves.
- Per-jurisdiction CSV / report formatters that do not exist yet
  (1256, MiFID II, etc.) — pick them up as separate issues.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from engine.core.tax.reports.carryover import (
    CapitalLossCarryover,
    apply_carryover,
)
from engine.core.tax.reports.cgt_carryover import (
    CgtCarryover,
    apply_cgt_carryover,
)
from engine.core.tax.reports.form_1099b import (
    LotDisposition,
    generate_1099b_rows,
)
from engine.core.tax.reports.france_pfu import PfuDisposal, summarize_pfu
from engine.core.tax.reports.hmrc_cgt import CgtDisposal, summarize_cgt
from engine.core.tax.reports.kest import (
    AssetClass,
    KestDisposal,
    summarize_kest,
)
from engine.core.tax.reports.kest_carryover import (
    KestCarryover,
    apply_kest_carryover,
)
from engine.core.tax.reports.pfu_carryover import (
    PfuCarryover,
    apply_pfu_carryover,
)
from engine.core.tax.reports.schedule_d import summarize_schedule_d

JurisdictionSummary = "ScheduleDSummary | CgtSummary | KestSummary | PfuSummary"

_TWOPLACES = Decimal("0.01")


@dataclass(frozen=True)
class TaxableDisposal:
    """Jurisdiction-neutral sale-event record.

    Mirrors the smallest set of fields every supported jurisdiction
    needs: a description (broker label / symbol), the acquisition and
    disposition dates, and the proceeds + cost in the *jurisdiction's*
    currency. Multi-currency conversion is the caller's responsibility.
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


class UnsupportedJurisdictionError(ValueError):
    """Raised when ``code`` does not match any supported jurisdiction."""


def report_for_jurisdiction(
    code: str,
    disposals: list[TaxableDisposal],
):
    """Dispatch ``disposals`` to the right per-jurisdiction summariser.

    ``code`` is the jurisdiction's two-letter slug (case-insensitive):
    ``US``, ``GB``, ``DE``, ``FR``. Anything else raises
    :class:`UnsupportedJurisdictionError` listing the supported codes.
    """
    norm = code.upper()
    if norm == "US":
        rows = generate_1099b_rows([_to_us(d) for d in disposals])
        return summarize_schedule_d(rows)
    if norm == "GB":
        return summarize_cgt([_to_gb(d) for d in disposals])
    if norm == "DE":
        return summarize_kest([_to_de(d) for d in disposals])
    if norm == "FR":
        return summarize_pfu([_to_fr(d) for d in disposals])
    raise UnsupportedJurisdictionError(f"unknown jurisdiction {code!r}; supported: US, GB, DE, FR")


# ---------------------------------------------------------------------------
# Per-jurisdiction adapters
# ---------------------------------------------------------------------------


def _to_us(d: TaxableDisposal) -> LotDisposition:
    return LotDisposition(
        description=d.description,
        acquired=d.acquired,
        sold=d.disposed,
        proceeds=d.proceeds,
        cost_basis=d.cost,
    )


def _to_gb(d: TaxableDisposal) -> CgtDisposal:
    return CgtDisposal(
        description=d.description,
        acquired=d.acquired,
        disposed=d.disposed,
        proceeds=d.proceeds,
        cost=d.cost,
    )


def _to_de(d: TaxableDisposal) -> KestDisposal:
    return KestDisposal(
        description=d.description,
        acquired=d.acquired,
        disposed=d.disposed,
        proceeds=d.proceeds,
        cost=d.cost,
        asset_class=AssetClass.EQUITY,
    )


def _to_fr(d: TaxableDisposal) -> PfuDisposal:
    return PfuDisposal(
        description=d.description,
        acquired=d.acquired,
        disposed=d.disposed,
        proceeds=d.proceeds,
        cost=d.cost,
    )


def flatten_summary_to_csv(summary: Any) -> str:
    """Render any frozen-dataclass tax summary as a 2-row CSV.

    Walks every field of ``summary``; nested dataclasses become
    dotted column names (``short_term.gain_loss``); ``Decimal`` /
    ``date`` / ``Enum`` values are stringified. The result is a
    deterministic header row + one values row, suitable for a CPA
    paste-in or a spreadsheet import.

    Lists and tuples are joined with ``;`` so a single CSV cell can
    carry them — none of the current summaries use them, but the
    helper is forward-compatible with whatever the next jurisdiction
    needs.
    """
    columns: list[str] = []
    values: list[str] = []
    _walk(summary, prefix="", columns=columns, values=values)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerow(values)
    return buf.getvalue()


def _walk(
    obj: Any,
    *,
    prefix: str,
    columns: list[str],
    values: list[str],
) -> None:
    if is_dataclass(obj) and not isinstance(obj, type):
        for f in fields(obj):
            child = getattr(obj, f.name)
            child_prefix = f"{prefix}{f.name}" if not prefix else f"{prefix}.{f.name}"
            _walk(
                child,
                prefix=child_prefix,
                columns=columns,
                values=values,
            )
        return
    columns.append(prefix or "value")
    values.append(_render_scalar(obj))


def _render_scalar(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list | tuple):
        return ";".join(_render_scalar(x) for x in value)
    return "" if value is None else str(value)


JurisdictionCarryover = "CapitalLossCarryover | CgtCarryover | KestCarryover | PfuCarryover"
CarryoverApplication = "CapitalLossApplication | CgtApplication | KestApplication | PfuApplication"


def carryover_for_jurisdiction(
    code: str,
    disposals: list[TaxableDisposal],
    prior: Any = None,
    *,
    current_year: int | None = None,
    **kwargs: Any,
):
    """Run the per-jurisdiction carryover applier for ``code``.

    ``prior`` must match the jurisdiction's carryover record type:

    - US → :class:`CapitalLossCarryover` (or ``None``).
    - GB → :class:`CgtCarryover` (or ``None``).
    - DE → :class:`KestCarryover` (or ``None``).
    - FR → :class:`PfuCarryover` (or ``None``).

    ``current_year`` is required for FR (used to date new vintages and
    expire ones older than ten years). It is ignored by every other
    jurisdiction.

    Extra ``kwargs`` are forwarded to the underlying applier:

    - US: ``deductible_cap``.
    - GB: ``annual_exempt_amount``.
    - DE: ``allowance``, ``church_tax_rate``.
    - FR: (none beyond ``current_year``).

    The return type is the union of the four ``Application`` records;
    callers branch on it via ``isinstance``.
    """
    norm = code.upper()
    if norm == "US":
        _ensure_prior(prior, CapitalLossCarryover)
        rows = generate_1099b_rows([_to_us(d) for d in disposals])
        summary = summarize_schedule_d(rows)
        return apply_carryover(summary, prior, **kwargs)
    if norm == "GB":
        _ensure_prior(prior, CgtCarryover)
        return apply_cgt_carryover([_to_gb(d) for d in disposals], prior, **kwargs)
    if norm == "DE":
        _ensure_prior(prior, KestCarryover)
        return apply_kest_carryover([_to_de(d) for d in disposals], prior, **kwargs)
    if norm == "FR":
        _ensure_prior(prior, PfuCarryover)
        if current_year is None:
            raise ValueError(
                "FR carryover requires current_year (used for vintage tagging + 10-year expiry)"
            )
        return apply_pfu_carryover(
            [_to_fr(d) for d in disposals],
            prior,
            current_year=current_year,
            **kwargs,
        )
    raise UnsupportedJurisdictionError(f"unknown jurisdiction {code!r}; supported: US, GB, DE, FR")


def _ensure_prior(prior: Any, expected: type) -> None:
    """Raise :class:`TypeError` if ``prior`` is set but not the right
    carryover record type for the jurisdiction. ``None`` is always
    accepted (each applier defaults to a zero carryover)."""
    if prior is None:
        return
    if not isinstance(prior, expected):
        raise TypeError(f"prior must be {expected.__name__} or None, got {type(prior).__name__}")


__all__ = [
    "CarryoverApplication",
    "JurisdictionCarryover",
    "TaxableDisposal",
    "UnsupportedJurisdictionError",
    "carryover_for_jurisdiction",
    "flatten_summary_to_csv",
    "report_for_jurisdiction",
]
