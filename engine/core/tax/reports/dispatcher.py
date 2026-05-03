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

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

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
from engine.core.tax.reports.schedule_d import summarize_schedule_d

if TYPE_CHECKING:
    from engine.core.tax.reports.france_pfu import PfuSummary
    from engine.core.tax.reports.hmrc_cgt import CgtSummary
    from engine.core.tax.reports.kest import KestSummary
    from engine.core.tax.reports.schedule_d import ScheduleDSummary

JurisdictionSummary = (
    "ScheduleDSummary | CgtSummary | KestSummary | PfuSummary"
)

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
    raise UnsupportedJurisdictionError(
        f"unknown jurisdiction {code!r}; supported: US, GB, DE, FR"
    )


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


__all__ = [
    "TaxableDisposal",
    "UnsupportedJurisdictionError",
    "report_for_jurisdiction",
]
