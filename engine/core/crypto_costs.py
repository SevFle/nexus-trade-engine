"""Crypto / multi-asset cost helpers (gh#96 follow-up).

Pure-function helpers for cost components that show up on crypto and
forex desks but not in the equities-only ``regulatory_fees`` /
``execution_costs`` / ``holding_costs`` modules.

Components covered:

- Perpetual-futures funding payment (Binance / Bybit / dYdX style)
- FX conversion with bps spread
- AMM constant-product impermanent loss (Uniswap V2 / SushiSwap)

Out of scope (explicit follow-ups):
- Cross-chain bridge fees + slippage.
- LP fee revenue accrual on the *positive* side of liquidity
  provision.
- Gas fees on individual transactions (highly chain-specific —
  caller passes the gas cost in tokens; conversion to USD belongs
  in the caller's settlement layer).
- Stake / unstake unbonding penalties.
- Funding-rate prediction. The helper here computes the *settled*
  payment given a known rate; predicting the next rate is a
  separate model.
"""

from __future__ import annotations

import math
from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# Standard 8-hour funding interval used by every major perpetual-
# futures venue. Some venues (e.g. dYdX) have shifted to 1-hour
# intervals; operators override via ``hours`` kwarg.
DEFAULT_FUNDING_INTERVAL_HOURS: int = 8


def perpetual_funding_payment(
    notional: Decimal,
    funding_rate: Decimal,
    *,
    side: str,
    hours: int = DEFAULT_FUNDING_INTERVAL_HOURS,
) -> Decimal:
    """Funding payment for one settlement interval on a perpetual swap.

    ``notional`` is the absolute USD position size (positive Decimal).
    ``funding_rate`` is the *signed* funding rate per interval (e.g.
    ``Decimal("0.0001")`` for 1 bp per 8-hour interval — a positive
    rate means longs pay shorts).

    ``side`` is ``"long"`` or ``"short"``. Returns the *signed* payment
    from the perspective of the holder: positive = the holder receives
    cash, negative = the holder pays. This convention lets operators
    sum funding payments straight into a daily PnL ledger.

    The full one-day payment for the standard 8-hour cadence is
    ``3 * per-interval rate * notional``; pass ``hours=24`` if the
    caller's funding rate is already daily.
    """
    if notional < 0:
        raise ValueError("notional must be non-negative")
    if hours <= 0:
        raise ValueError("hours must be positive")
    if side not in {"long", "short"}:
        raise ValueError("side must be 'long' or 'short'")
    raw = (notional * funding_rate).quantize(_TWOPLACES)
    # Long PAYS when funding_rate > 0; short RECEIVES.
    if side == "long":
        return -raw
    return raw


def fx_conversion(
    amount: Decimal,
    rate: Decimal,
    *,
    fee_bps: Decimal = Decimal("10"),
) -> tuple[Decimal, Decimal]:
    """Convert ``amount`` of source-currency at ``rate`` minus a bps fee.

    Returns ``(converted_amount, fee_in_target_currency)``. The fee is
    deducted from the converted amount: ``converted = amount * rate *
    (1 - fee_bps / 10000)``; ``fee = amount * rate - converted``.

    Default fee is 10 bps (0.1 %) — typical retail-broker FX spread.
    Operators override per venue. Both return values are quantised to
    two decimals.
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if rate < 0:
        raise ValueError("rate must be non-negative")
    if fee_bps < 0:
        raise ValueError("fee_bps must be non-negative")
    gross = amount * rate
    fee = (gross * fee_bps / Decimal("10000")).quantize(_TWOPLACES)
    net = (gross - fee).quantize(_TWOPLACES)
    return net, fee


def constant_product_impermanent_loss(price_ratio: float) -> float:
    """Impermanent loss as a *positive fraction* of the held value.

    For a Uniswap-V2-style constant-product AMM (x · y = k) holding a
    50/50 LP position, the impermanent loss versus simply holding the
    underlying assets is::

        IL = 2 · sqrt(p) / (1 + p) - 1

    where ``p`` is the price ratio (new price / old price) of one of
    the two assets. The function returns ``-IL`` so the caller sees a
    *positive* loss fraction (e.g. ``0.0203`` for 2.03 % at a 50 %
    price move).

    Returns ``0.0`` when ``price_ratio == 1`` (no loss).

    Symmetric in ``p``: 0.5 (price halved) and 2.0 (price doubled)
    produce the same IL.
    """
    if price_ratio < 0:
        raise ValueError("price_ratio must be non-negative")
    if price_ratio == 0:
        # Asset went to zero — LP keeps half the original position
        # value, so IL is approximately 50 % vs holding.
        return 0.5
    if price_ratio == 1.0:
        return 0.0
    il_signed = 2.0 * math.sqrt(price_ratio) / (1.0 + price_ratio) - 1.0
    return -il_signed


__all__ = [
    "DEFAULT_FUNDING_INTERVAL_HOURS",
    "constant_product_impermanent_loss",
    "fx_conversion",
    "perpetual_funding_payment",
]
