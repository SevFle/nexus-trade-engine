"""Regulatory report generation (gh#155).

Today this exposes the US Form 1099-B / Schedule D row generator.
Other jurisdictions (1256 contracts, MiFID II, HMRC CGT, KESt) are
explicit follow-ups — each adds its own row schema and serialiser
under this package.
"""

from engine.core.tax.reports.form_1099b import (
    HoldingTerm,
    LotDisposition,
    Schedule1099BRow,
    generate_1099b_rows,
    rows_to_csv,
)

__all__ = [
    "HoldingTerm",
    "LotDisposition",
    "Schedule1099BRow",
    "generate_1099b_rows",
    "rows_to_csv",
]
