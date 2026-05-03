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
from engine.core.tax.reports.cgt_carryover import (
    CgtApplication,
    CgtCarryover,
    apply_cgt_carryover,
)
from engine.core.tax.reports.hmrc_cgt import (
    ANNUAL_EXEMPT_AMOUNT_2024_25,
    CgtDisposal,
    CgtSummary,
    disposals_to_csv,
    summarize_cgt,
)
from engine.core.tax.reports.dispatcher import (
    TaxableDisposal,
    UnsupportedJurisdictionError,
    carryover_for_jurisdiction,
    flatten_summary_to_csv,
    report_for_jurisdiction,
)
from engine.core.tax.reports.france_pfu import (
    PFU_INCOME_TAX_RATE,
    PFU_SOCIAL_CHARGES_RATE,
    PFU_TOTAL_RATE,
    PfuDisposal,
    PfuSummary,
    summarize_pfu,
)
from engine.core.tax.reports.pfu_carryover import (
    PfuApplication,
    PfuCarryover,
    PfuLossVintage,
    apply_pfu_carryover,
)
from engine.core.tax.reports.kest import (
    CHURCH_TAX_RATE_BAYERN_BW,
    CHURCH_TAX_RATE_OTHER,
    KEST_RATE,
    SOLZ_RATE,
    SPARER_PAUSCHBETRAG_2023,
    SPARER_PAUSCHBETRAG_2024,
    SPARER_PAUSCHBETRAG_JOINT_2023,
    SPARER_PAUSCHBETRAG_JOINT_2024,
    AssetClass,
    KestDisposal,
    KestSummary,
    summarize_kest,
)
from engine.core.tax.reports.kest_carryover import (
    KestApplication,
    KestCarryover,
    apply_kest_carryover,
)
from engine.core.tax.reports.form_6781 import (
    LONG_TERM_PCT,
    SHORT_TERM_PCT,
    Form6781Summary,
    Section1256Contract,
    contracts_to_csv,
    summarize_form6781,
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
    "CHURCH_TAX_RATE_BAYERN_BW",
    "CHURCH_TAX_RATE_OTHER",
    "DEDUCTIBLE_CAP_DEFAULT",
    "DEDUCTIBLE_CAP_MFS",
    "Form6781Summary",
    "KEST_RATE",
    "LONG_TERM_PCT",
    "PFU_INCOME_TAX_RATE",
    "SHORT_TERM_PCT",
    "Section1256Contract",
    "PFU_SOCIAL_CHARGES_RATE",
    "PFU_TOTAL_RATE",
    "PfuApplication",
    "PfuCarryover",
    "PfuDisposal",
    "PfuLossVintage",
    "PfuSummary",
    "SOLZ_RATE",
    "SPARER_PAUSCHBETRAG_2023",
    "SPARER_PAUSCHBETRAG_2024",
    "SPARER_PAUSCHBETRAG_JOINT_2023",
    "SPARER_PAUSCHBETRAG_JOINT_2024",
    "AssetClass",
    "CapitalLossApplication",
    "CapitalLossCarryover",
    "CgtApplication",
    "CgtCarryover",
    "CgtDisposal",
    "CgtSummary",
    "HoldingTerm",
    "KestApplication",
    "KestCarryover",
    "KestDisposal",
    "KestSummary",
    "LotDisposition",
    "Schedule1099BRow",
    "ScheduleDPartTotal",
    "ScheduleDSummary",
    "TaxableDisposal",
    "UnsupportedJurisdictionError",
    "apply_carryover",
    "apply_cgt_carryover",
    "apply_kest_carryover",
    "apply_pfu_carryover",
    "carryover_for_jurisdiction",
    "contracts_to_csv",
    "disposals_to_csv",
    "flatten_summary_to_csv",
    "generate_1099b_rows",
    "report_for_jurisdiction",
    "rows_to_csv",
    "summarize_cgt",
    "summarize_form6781",
    "summarize_kest",
    "summarize_pfu",
    "summarize_schedule_d",
    "summary_to_csv",
]
