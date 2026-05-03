"""Holding-cost helpers (gh#96 follow-up).

Pure-function helpers for costs that accrue *while a position is
open* — distinct from execution costs that fire at trade-event time.

Components covered (numbers from gh#96 taxonomy):

- C2  hard_to_borrow_cost                    — short-position daily borrow fee
- C3  dividend_payment / reinvested_shares   — dividend handling

Out of scope (explicit follow-ups):
- Stock-loan rebate (institutional negative-fee tier when borrow
  demand drops below supply).
- C4 ADR custody fees (account-level, separate ledger).
- C5 Foreign withholding tax on cross-border dividends (handled by
  jurisdiction engine).
- Special / qualified dividend distinction (caller passes the gross
  amount; tax treatment lives in the tax module).
"""

from __future__ import annotations

from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")
_FOURPLACES = Decimal("0.0001")

DAYS_PER_YEAR: int = 365


def hard_to_borrow_cost(
    short_market_value: Decimal,
    annual_rate: Decimal,
    *,
    days: int = 1,
    days_per_year: int = DAYS_PER_YEAR,
) -> Decimal:
    """Daily hard-to-borrow accrual for a short position.

    ``short_market_value`` is the *current market value* of the short
    (positive Decimal). ``annual_rate`` is the simple annual HTB rate
    as a fraction (e.g. ``Decimal("0.15")`` for 15 % APR — typical for
    moderately-hard-to-borrow names; severely-restricted names can
    exceed 100 %).

    Returns the accrued cost in USD over ``days`` calendar days.
    """
    if short_market_value < 0:
        raise ValueError("short_market_value must be non-negative")
    if annual_rate < 0:
        raise ValueError("annual_rate must be non-negative")
    if days < 0:
        raise ValueError("days must be non-negative")
    if days_per_year <= 0:
        raise ValueError("days_per_year must be positive")
    daily_rate = annual_rate / Decimal(days_per_year)
    return (short_market_value * daily_rate * Decimal(days)).quantize(_TWOPLACES)


def dividend_payment(
    shares_held: Decimal,
    dividend_per_share: Decimal,
) -> Decimal:
    """Cash dividend received: ``shares_held * dividend_per_share``.

    Both inputs are non-negative. Returns USD quantised to the cent.
    Operators apply withholding-tax adjustments separately via the
    jurisdiction engine; this helper is the gross dividend.
    """
    if shares_held < 0:
        raise ValueError("shares_held must be non-negative")
    if dividend_per_share < 0:
        raise ValueError("dividend_per_share must be non-negative")
    return (shares_held * dividend_per_share).quantize(_TWOPLACES)


def reinvested_shares(
    cash_amount: Decimal,
    reinvestment_price: Decimal,
    *,
    fractional: bool = True,
) -> Decimal:
    """Number of shares purchased under a DRIP at ``reinvestment_price``.

    With ``fractional=True`` (the default — most US brokers' DRIPs
    support fractional shares), returns the exact share count to four
    decimal places. With ``fractional=False`` (some broker programs
    or non-US accounts), truncates to the integer share count and the
    caller handles the residual cash.

    Returns 0 when ``cash_amount`` is 0; raises on negative inputs or
    non-positive ``reinvestment_price``.
    """
    if cash_amount < 0:
        raise ValueError("cash_amount must be non-negative")
    if reinvestment_price <= 0:
        raise ValueError("reinvestment_price must be positive")
    raw = cash_amount / reinvestment_price
    if fractional:
        return raw.quantize(_FOURPLACES)
    # Integer truncation: drop everything after the decimal point.
    return Decimal(int(raw))


def reinvestment_residual_cash(
    cash_amount: Decimal,
    reinvestment_price: Decimal,
    shares_purchased: Decimal,
) -> Decimal:
    """Cash left over after a DRIP purchase.

    Useful when ``fractional=False`` truncates the share count and the
    broker returns the remainder to cash. With ``fractional=True``
    this typically returns ``0.00``.
    """
    if cash_amount < 0:
        raise ValueError("cash_amount must be non-negative")
    if reinvestment_price < 0:
        raise ValueError("reinvestment_price must be non-negative")
    if shares_purchased < 0:
        raise ValueError("shares_purchased must be non-negative")
    spent = (shares_purchased * reinvestment_price).quantize(_TWOPLACES)
    residual = (cash_amount - spent).quantize(_TWOPLACES)
    if residual < 0:
        raise ValueError("shares_purchased exceeds what cash_amount can buy at reinvestment_price")
    return residual


__all__ = [
    "DAYS_PER_YEAR",
    "dividend_payment",
    "hard_to_borrow_cost",
    "reinvested_shares",
    "reinvestment_residual_cash",
]
