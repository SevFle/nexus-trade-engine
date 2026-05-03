"""Regulatory report generation (gh#155).

Today this exposes the US Form 1099-B / Schedule D row generator.
Other jurisdictions (1256 contracts, MiFID II, HMRC CGT, KESt) are
explicit follow-ups — each adds its own row schema and serialiser
under this package.
"""

from engine.core.tax.reports.carryover import (
    DEDUCTIBLE_CAP_DEFAULT,
    DEDUCTIBLE_CAP_MFS,
    CapitalLossApplication,
    CapitalLossCarryover,
    apply_carryover,
)
from engine.core.tax.reports.hmrc_cgt import (
    ANNUAL_EXEMPT_AMOUNT_2024_25,
    CgtDisposal,
    CgtSummary,
    disposals_to_csv,
    summarize_cgt,
)
from engine.core.tax.reports.form_1099b import (
    HoldingTerm,
    LotDisposition,
    Schedule1099BRow,
    generate_1099b_rows,
    rows_to_csv,
)
from engine.core.tax.reports.schedule_d import (
    ScheduleDPartTotal,
    ScheduleDSummary,
    summarize_schedule_d,
    summary_to_csv,
)

__all__ = [
    "ANNUAL_EXEMPT_AMOUNT_2024_25",
    "DEDUCTIBLE_CAP_DEFAULT",
    "DEDUCTIBLE_CAP_MFS",
    "CapitalLossApplication",
    "CapitalLossCarryover",
    "CgtDisposal",
    "CgtSummary",
    "HoldingTerm",
    "LotDisposition",
    "Schedule1099BRow",
    "ScheduleDPartTotal",
    "ScheduleDSummary",
    "apply_carryover",
    "disposals_to_csv",
    "generate_1099b_rows",
    "rows_to_csv",
    "summarize_cgt",
    "summarize_schedule_d",
    "summary_to_csv",
]
