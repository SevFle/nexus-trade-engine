"""Structured legal disclaimer and risk-disclosure content models.

This module is intentionally **database-free**: disclaimers are static,
editorial legal content that changes rarely and must remain available even
before the persistence layer is populated (e.g. during pre-login notice
screens and onboarding). Defining them as structured data lets the same
content be rendered consistently across the API, the acceptance flow, and
future client surfaces, and keeps the content reviewable in source control.

Four canonical categories are exposed:

* ``trading_risk``     — inherent risks of trading securities / derivatives.
* ``wash_sale``        — the IRS wash-sale rule and its effect on losses.
* ``tax_implications`` — general tax-treatment disclaimers (not advice).
* ``general``          — platform-wide notices not tied to one domain.

The accessors are pure and side-effect free and always return defensive
(deep) copies so callers cannot mutate the module's source-of-truth content.
They are consumed by the public API layer in :mod:`engine.api.legal`.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LAST_UPDATED",
    "Disclaimer",
    "DisclaimerCategory",
    "DisclaimerListResponse",
    "DisclaimerReference",
    "DisclaimerSeverity",
    "RiskDisclosureResponse",
    "RiskFactor",
    "get_all_disclaimers",
    "get_disclaimers_by_category",
    "get_risk_disclosure",
    "list_categories",
]

# Single source of truth for "last updated". Bump this when editorial content
# changes so clients can cache / version-gate the payload deterministically.
#
# Deliberately pinned to a date in the past so the invariant
# ``LAST_UPDATED <= date.today()`` always holds regardless of the host clock;
# bump it forward when the editorial content is materially revised.
LAST_UPDATED = date(2025, 7, 1)


class DisclaimerCategory(StrEnum):
    """Canonical disclaimer groupings surfaced by the API."""

    TRADING_RISK = "trading_risk"
    WASH_SALE = "wash_sale"
    TAX_IMPLICATIONS = "tax_implications"
    GENERAL = "general"


class DisclaimerSeverity(StrEnum):
    """How prominently a disclaimer should be surfaced to users."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DisclaimerReference(BaseModel):
    """An external citation backing a disclaimer statement."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Human-readable reference title.")
    url: str | None = Field(
        default=None,
        description="Optional link to the authoritative source.",
    )


class Disclaimer(BaseModel):
    """A single structured disclaimer statement.

    A disclaimer combines a short ``summary`` (safe to show inline) with an
    optional ordered ``details`` list (shown on disclosure screens) and
    optional ``references``. Every field is validated by pydantic and the
    model forbids extras so unknown fields are rejected explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, machine-readable disclaimer identifier.")
    category: DisclaimerCategory
    title: str
    severity: DisclaimerSeverity = DisclaimerSeverity.WARNING
    summary: str
    details: list[str] = Field(default_factory=list)
    references: list[DisclaimerReference] = Field(default_factory=list)


class DisclaimerListResponse(BaseModel):
    """Response body for ``GET /api/v1/legal/disclaimers``.

    ``categories`` describes the categories that are *represented in the
    returned ``disclaimers`` list* (in canonical enum order), so when a
    category filter is applied it shrinks to that single category.
    """

    model_config = ConfigDict(extra="forbid")

    disclaimers: list[Disclaimer]
    categories: list[DisclaimerCategory]
    count: int = Field(description="Number of disclaimers in this response.")
    last_updated: date


class RiskFactor(BaseModel):
    """A discrete, actionable risk factor within the risk disclosure."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    severity: DisclaimerSeverity = DisclaimerSeverity.WARNING


class RiskDisclosureResponse(BaseModel):
    """Response body for ``GET /api/v1/legal/risk-disclosures``.

    Combines a plain-language ``overview``, a list of discrete ``risk_factors``,
    and the ``related_disclaimers`` (the structured disclaimer entries that
    elaborate on the most loss-relevant risk areas) so a client can render the
    full disclosure surface from one request.
    """

    model_config = ConfigDict(extra="forbid")

    overview: str
    risk_factors: list[RiskFactor]
    related_disclaimers: list[Disclaimer]
    last_updated: date


# ---------------------------------------------------------------------------
# Source-of-truth content
#
# Defined as a tuple so the registry is immutable. Accessors hand out deep
# copies, so even if a caller mutates a returned model the registry is safe.
# ---------------------------------------------------------------------------

_DISCLAIMERS: tuple[Disclaimer, ...] = (
    Disclaimer(
        id="trading-risk-substantial-loss",
        category=DisclaimerCategory.TRADING_RISK,
        title="Trading Involves a Substantial Risk of Loss",
        severity=DisclaimerSeverity.CRITICAL,
        summary=(
            "Trading securities, options, futures, and other instruments involves a "
            "substantial risk of loss and is not suitable for every investor."
        ),
        details=[
            "You can lose some or all of your invested capital.",
            "Past performance is not indicative of future results.",
            "Leverage and derivatives can magnify both gains and losses.",
            "Market conditions, liquidity, and system availability may change without notice.",
        ],
        references=[
            DisclaimerReference(
                label="SEC Investor Bulletins",
                url="https://www.sec.gov/oiea/investor-alerts-and-bulletins",
            ),
        ],
    ),
    Disclaimer(
        id="trading-risk-not-advice",
        category=DisclaimerCategory.TRADING_RISK,
        title="Information Is Not Investment Advice",
        severity=DisclaimerSeverity.WARNING,
        summary=(
            "Nothing provided by the platform constitutes investment, financial, "
            "legal, or tax advice, nor a recommendation or solicitation to buy or "
            "sell any security."
        ),
        details=[
            "Outputs are generated by automated strategies and models.",
            "You are solely responsible for your trading and investment decisions.",
            "The operator is not a registered investment adviser or broker-dealer.",
        ],
    ),
    Disclaimer(
        id="wash-sale-rule",
        category=DisclaimerCategory.WASH_SALE,
        title="Wash-Sale Rule May Disallow Losses",
        severity=DisclaimerSeverity.WARNING,
        summary=(
            "Selling a security at a loss and repurchasing a substantially identical "
            "security within 30 days can trigger the IRS wash-sale rule, disallowing "
            "the loss for tax purposes."
        ),
        details=[
            "The wash-sale window spans 30 days before and after the sale.",
            "Disallowed losses are added to the cost basis of the replacement position.",
            "The engine tracks wash sales for reporting but does not provide tax advice.",
        ],
        references=[
            DisclaimerReference(
                label="IRS Publication 550",
                url="https://www.irs.gov/publications/p550",
            ),
        ],
    ),
    Disclaimer(
        id="tax-implications-not-advice",
        category=DisclaimerCategory.TAX_IMPLICATIONS,
        title="Tax Treatment Depends on Individual Circumstances",
        severity=DisclaimerSeverity.WARNING,
        summary=(
            "Tax laws are complex and the tax treatment of any transaction depends on "
            "your individual facts and circumstances. This platform does not provide "
            "tax, legal, or accounting advice."
        ),
        details=[
            "Consult a qualified tax professional before acting on any report.",
            "Short-term and long-term gains are generally taxed at different rates.",
            "State and local tax rules may differ from federal rules.",
        ],
    ),
    Disclaimer(
        id="general-no-warranty",
        category=DisclaimerCategory.GENERAL,
        title="No Warranty; Information Provided 'As Is'",
        severity=DisclaimerSeverity.INFO,
        summary=(
            "The platform and its outputs are provided on an 'as is' basis without "
            "warranties of any kind, either express or implied."
        ),
        details=[
            "Backtested or simulated results do not guarantee future performance.",
            "Data may contain errors, delays, or gaps.",
            "The operator disclaims liability for decisions made based on the platform.",
        ],
    ),
)

_OVERVIEW = (
    "Trading and investing in financial markets carry significant risk. The "
    "information below summarises the most material risks users should understand "
    "before using the automated trading, backtesting, or portfolio tools provided "
    "by the platform. This disclosure is provided for informational purposes only "
    "and does not constitute investment, tax, legal, or accounting advice."
)

_RISK_FACTORS: tuple[RiskFactor, ...] = (
    RiskFactor(
        id="capital-loss",
        title="Risk of Total Capital Loss",
        description=(
            "You may lose some or all of the capital you invest. Never invest money "
            "you cannot afford to lose."
        ),
        severity=DisclaimerSeverity.CRITICAL,
    ),
    RiskFactor(
        id="leverage-margin",
        title="Leverage and Margin Risk",
        description=(
            "Trading on margin or with derivatives amplifies losses and can result in "
            "owing more than your initial deposit."
        ),
        severity=DisclaimerSeverity.CRITICAL,
    ),
    RiskFactor(
        id="backtest-limitations",
        title="Backtest and Simulation Limitations",
        description=(
            "Backtested results are hypothetical and do not represent actual trading. "
            "They are subject to limitations such as look-ahead bias, slippage, and "
            "transaction-cost assumptions that may not reflect live conditions."
        ),
        severity=DisclaimerSeverity.WARNING,
    ),
    RiskFactor(
        id="system-connectivity",
        title="System and Connectivity Risk",
        description=(
            "Automated strategies depend on network and infrastructure availability. "
            "Outages, latency, or data-feed errors can cause orders to be delayed, "
            "rejected, or executed at unintended prices."
        ),
        severity=DisclaimerSeverity.WARNING,
    ),
    RiskFactor(
        id="tax-regulatory",
        title="Tax and Regulatory Risk",
        description=(
            "Tax treatment is uncertain and jurisdiction-dependent. Regulatory changes "
            "may affect the legality, taxation, or availability of certain strategies. "
            "Consult qualified professionals before acting."
        ),
        severity=DisclaimerSeverity.WARNING,
    ),
)

# Categories directly related to the risk of financial loss — surfaced under
# ``related_disclaimers`` so the risk-disclosure endpoint can link back to the
# detailed structured disclaimers for each loss-relevant area.
_RISK_RELATED_CATEGORIES: frozenset[DisclaimerCategory] = frozenset(
    {
        DisclaimerCategory.TRADING_RISK,
        DisclaimerCategory.WASH_SALE,
        DisclaimerCategory.TAX_IMPLICATIONS,
    }
)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

# Precompute canonical enum ordering for stable output.
_CATEGORY_ORDER: dict[DisclaimerCategory, int] = {
    category: index for index, category in enumerate(DisclaimerCategory)
}


def _coerce_category(category: DisclaimerCategory | str) -> DisclaimerCategory:
    """Accept either the enum or its string value; reject anything else."""
    if isinstance(category, DisclaimerCategory):
        return category
    try:
        return DisclaimerCategory(category)
    except ValueError as exc:
        raise ValueError(f"Unknown disclaimer category: {category!r}") from exc


def list_categories() -> list[DisclaimerCategory]:
    """Return the categories that have at least one disclaimer, in canonical order."""
    present = {disclaimer.category for disclaimer in _DISCLAIMERS}
    return [
        category
        for category in DisclaimerCategory
        if category in present
    ]


def get_all_disclaimers() -> list[Disclaimer]:
    """Return every disclaimer as a defensive deep copy, in registry order."""
    return [disclaimer.model_copy(deep=True) for disclaimer in _DISCLAIMERS]


def get_disclaimers_by_category(category: DisclaimerCategory | str) -> list[Disclaimer]:
    """Return the disclaimers for a single category.

    ``category`` may be the enum member or its string value; an unknown value
    raises :class:`ValueError` so the API layer can map it to a clean 422 /
    400 response rather than silently returning an empty list for a typo.
    """
    resolved = _coerce_category(category)
    return [
        disclaimer.model_copy(deep=True)
        for disclaimer in _DISCLAIMERS
        if disclaimer.category == resolved
    ]


def _categories_present(disclaimers: list[Disclaimer]) -> list[DisclaimerCategory]:
    """Distinct categories among ``disclaimers`` in canonical enum order."""
    seen: set[DisclaimerCategory] = set()
    ordered: list[DisclaimerCategory] = []
    for disclaimer in disclaimers:
        if disclaimer.category not in seen:
            seen.add(disclaimer.category)
            ordered.append(disclaimer.category)
    ordered.sort(key=lambda category: _CATEGORY_ORDER[category])
    return ordered


def build_disclaimer_list_response(
    category: DisclaimerCategory | str | None = None,
) -> DisclaimerListResponse:
    """Build the disclaimers list response, optionally filtered by category.

    Centralised here so the API layer and any non-HTTP caller (e.g. an MCP
    resource or CLI) produce identical payloads.
    """
    if category is None:
        disclaimers = get_all_disclaimers()
    else:
        disclaimers = get_disclaimers_by_category(category)
    return DisclaimerListResponse(
        disclaimers=disclaimers,
        categories=_categories_present(disclaimers),
        count=len(disclaimers),
        last_updated=LAST_UPDATED,
    )


def get_risk_disclosure() -> RiskDisclosureResponse:
    """Return the detailed, structured risk-disclosure payload."""
    related = [
        disclaimer.model_copy(deep=True)
        for disclaimer in _DISCLAIMERS
        if disclaimer.category in _RISK_RELATED_CATEGORIES
    ]
    return RiskDisclosureResponse(
        overview=_OVERVIEW,
        risk_factors=[factor.model_copy(deep=True) for factor in _RISK_FACTORS],
        related_disclaimers=related,
        last_updated=LAST_UPDATED,
    )
