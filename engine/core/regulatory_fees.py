"""US regulatory & holding-cost helpers (gh#96 follow-up).

Pure functions over individual trade attributes — operators compose
them inside their own cost-model adapter rather than calling the
aggregate :class:`engine.core.cost_model.DefaultCostModel` for every
component. The rates are *configurable* with sensible defaults pinned
to the FY2026 schedule; pass operator overrides when the SEC, FINRA
or exchange publishes a new rate.

Components covered (numbers from gh#96 taxonomy):

- A2  SEC Section 31 fee          — sells only, applied to gross proceeds
- A2  FINRA Trading Activity Fee  — sells only, per-share with cap
- A2  Options Regulatory Fee      — buys + sells, per-contract
- A3  OCC clearing fee             — per-contract (options)
- C1  Margin-interest daily accrual — borrowed_amount * rate / 365

Out of scope (explicit follow-ups):
- Per-exchange taker / maker rebates (varies by venue + tier).
- NSCC / DTC clearing & settlement granularity.
- Tax fees beyond US (handled by jurisdiction engine).
- Borrow-cost rates for short positions (separate from margin).
- ADR custody fees, dividend withholding, foreign tax credits.
"""

from __future__ import annotations

from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# FY2026 SEC Section 31 fee rate: $20.60 per million dollars of
# proceeds. Applies to *sells* only on US equity transactions.
SEC_SECTION_31_RATE_PER_MILLION_2026: Decimal = Decimal("20.60")

# FY2025 FINRA Trading Activity Fee: $0.000166 per share on sells,
# capped at $8.30 per trade.
FINRA_TAF_PER_SHARE_2025: Decimal = Decimal("0.000166")
FINRA_TAF_MAX_PER_TRADE_2025: Decimal = Decimal("8.30")

# Options Regulatory Fee FY2025: $0.02905 per contract, charged on
# both sides of the trade.
ORF_PER_CONTRACT_2025: Decimal = Decimal("0.02905")

# OCC clearing fee: $0.055 per contract.
OCC_CLEARING_FEE_PER_CONTRACT: Decimal = Decimal("0.055")

DAYS_PER_YEAR: int = 365


def sec_section_31_fee(
    proceeds: Decimal,
    *,
    side: str,
    rate_per_million: Decimal = SEC_SECTION_31_RATE_PER_MILLION_2026,
) -> Decimal:
    """SEC Section 31 fee on US equity *sells*. Buys produce zero.

    ``proceeds`` is the gross sale amount (price × quantity) in USD.
    Returns the fee in USD, quantised to the cent (the SEC rounds
    *up*, but at our precision a half-cent rounding is invisible —
    operators reconcile against the broker's clearing report).
    """
    if proceeds < 0:
        raise ValueError("proceeds must be non-negative")
    if rate_per_million < 0:
        raise ValueError("rate_per_million must be non-negative")
    if side != "sell":
        return _ZERO
    fee = proceeds * rate_per_million / Decimal("1000000")
    return fee.quantize(_TWOPLACES)


def finra_taf(
    quantity: int,
    *,
    side: str,
    per_share: Decimal = FINRA_TAF_PER_SHARE_2025,
    max_per_trade: Decimal = FINRA_TAF_MAX_PER_TRADE_2025,
) -> Decimal:
    """FINRA Trading Activity Fee on equity *sells*.

    ``quantity`` is the share count. Returns the fee in USD, capped at
    ``max_per_trade``. Buys produce zero.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if per_share < 0 or max_per_trade < 0:
        raise ValueError("rates must be non-negative")
    if side != "sell":
        return _ZERO
    fee = (Decimal(quantity) * per_share).quantize(_TWOPLACES)
    return min(fee, max_per_trade)


def options_regulatory_fee(
    contracts: int,
    *,
    per_contract: Decimal = ORF_PER_CONTRACT_2025,
) -> Decimal:
    """ORF fee — applies to both sides of an options trade.

    ``contracts`` is the option-contract count. Returns USD.
    """
    if contracts < 0:
        raise ValueError("contracts must be non-negative")
    if per_contract < 0:
        raise ValueError("per_contract must be non-negative")
    return (Decimal(contracts) * per_contract).quantize(_TWOPLACES)


def occ_clearing_fee(
    contracts: int,
    *,
    per_contract: Decimal = OCC_CLEARING_FEE_PER_CONTRACT,
) -> Decimal:
    """OCC clearing fee — per options contract, both sides.

    Distinct from :func:`options_regulatory_fee` (different rate; OCC
    is paid to the clearing house, ORF to OCC's regulator). Operators
    typically pass both for an options trade.
    """
    if contracts < 0:
        raise ValueError("contracts must be non-negative")
    if per_contract < 0:
        raise ValueError("per_contract must be non-negative")
    return (Decimal(contracts) * per_contract).quantize(_TWOPLACES)


def daily_margin_interest(
    borrowed_amount: Decimal,
    annual_rate: Decimal,
    *,
    days: int = 1,
    days_per_year: int = DAYS_PER_YEAR,
) -> Decimal:
    """Accrued margin interest over ``days`` calendar days.

    ``annual_rate`` is the simple annual percentage as a fraction (e.g.
    ``Decimal("0.085")`` for 8.5 % APR). Returns USD. ``days`` defaults
    to 1 (the typical daily-accrual cadence).
    """
    if borrowed_amount < 0:
        raise ValueError("borrowed_amount must be non-negative")
    if annual_rate < 0:
        raise ValueError("annual_rate must be non-negative")
    if days < 0:
        raise ValueError("days must be non-negative")
    if days_per_year <= 0:
        raise ValueError("days_per_year must be positive")
    daily_rate = annual_rate / Decimal(days_per_year)
    return (borrowed_amount * daily_rate * Decimal(days)).quantize(_TWOPLACES)


__all__ = [
    "DAYS_PER_YEAR",
    "FINRA_TAF_MAX_PER_TRADE_2025",
    "FINRA_TAF_PER_SHARE_2025",
    "OCC_CLEARING_FEE_PER_CONTRACT",
    "ORF_PER_CONTRACT_2025",
    "SEC_SECTION_31_RATE_PER_MILLION_2026",
    "daily_margin_interest",
    "finra_taf",
    "occ_clearing_fee",
    "options_regulatory_fee",
    "sec_section_31_fee",
]
