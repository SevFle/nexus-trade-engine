"""Execution cost helpers (gh#96 follow-up).

Pure-function cost components composable with the existing
:class:`engine.core.cost_model.DefaultCostModel`. Each helper returns
a USD ``Decimal`` quantised to the cent.

Components covered (numbers from gh#96 taxonomy):

- A3  exchange_taker_fee / exchange_maker_rebate — per-share venue fees
- A4  nscc_clearing_fee                          — per-side NSCC fee
- B1  half_spread_cost                           — half the bid/ask spread
- B5  opportunity_cost                           — unfilled-shares price drift

Out of scope (explicit follow-ups):
- DTC transfer fees (different fee schedule, account-level).
- Per-venue tier-aware maker rebates (operators thread their broker
  tier into ``rate_per_share``).
- Locked-up cost-of-carry on partial fills (different module).
"""

from __future__ import annotations

from decimal import Decimal

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0.00")

# NSCC continuous net-settlement fee schedule (FY2025): roughly
# $0.0002 per side. Operators reconcile against their clearing report
# via the ``per_side`` kwarg.
NSCC_FEE_PER_SIDE_2025: Decimal = Decimal("0.0002")

# Typical exchange taker fee on an aggressive order. Different venues
# (NYSE / NASDAQ / ARCA / BATS) have different rates; this default
# represents the high-water mark, $0.0030 per share.
DEFAULT_TAKER_FEE_PER_SHARE: Decimal = Decimal("0.0030")

# Typical maker rebate for adding liquidity, $0.0020 per share. Some
# venues pay you to post; others charge access. Operators override
# per-venue.
DEFAULT_MAKER_REBATE_PER_SHARE: Decimal = Decimal("0.0020")


def half_spread_cost(
    spread: Decimal,
    quantity: int,
) -> Decimal:
    """Cost of crossing half the bid/ask spread on an aggressive order.

    ``spread`` is the bid/ask spread in *price units* (e.g.
    ``Decimal("0.01")`` for a one-cent spread). The half-spread is the
    expected slippage when crossing a market order: a buy pays the
    ask, a sell hits the bid, and on average half the round-trip
    spread is paid each side.

    Returns the dollar cost; quantises to the cent. Negative inputs
    raise.
    """
    if spread < 0:
        raise ValueError("spread must be non-negative")
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    return ((spread / 2) * Decimal(quantity)).quantize(_TWOPLACES)


def nscc_clearing_fee(
    quantity: int,
    *,
    per_side: Decimal = NSCC_FEE_PER_SIDE_2025,
) -> Decimal:
    """NSCC continuous net-settlement fee for one side of a US equity
    trade.

    ``quantity`` is the share count for this side. Returns USD. The
    fee applies symmetrically to both buys and sells.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if per_side < 0:
        raise ValueError("per_side must be non-negative")
    return (Decimal(quantity) * per_side).quantize(_TWOPLACES)


def exchange_taker_fee(
    quantity: int,
    *,
    rate_per_share: Decimal = DEFAULT_TAKER_FEE_PER_SHARE,
) -> Decimal:
    """Per-share exchange taker fee (aggressive / liquidity-removing).

    Returns the dollar fee, quantised to the cent. Operators pass
    their venue-specific rate via ``rate_per_share``.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if rate_per_share < 0:
        raise ValueError("rate_per_share must be non-negative")
    return (Decimal(quantity) * rate_per_share).quantize(_TWOPLACES)


def exchange_maker_rebate(
    quantity: int,
    *,
    rate_per_share: Decimal = DEFAULT_MAKER_REBATE_PER_SHARE,
) -> Decimal:
    """Per-share exchange maker rebate (passive / liquidity-adding).

    Returns a *positive* dollar amount the venue pays the trader.
    Operators interpret it as a negative cost when summing the full
    cost stack. Defaults to $0.0020 per share but varies widely by
    venue and tier.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if rate_per_share < 0:
        raise ValueError("rate_per_share must be non-negative")
    return (Decimal(quantity) * rate_per_share).quantize(_TWOPLACES)


def opportunity_cost(
    unfilled_shares: int,
    price_change: Decimal,
) -> Decimal:
    """Implementation-shortfall opportunity cost on unfilled shares.

    ``price_change`` is the signed price drift between the decision
    timestamp and the cancel/expire timestamp, in *price units*. For
    a buy that didn't fill, a positive ``price_change`` means the
    market ran away — operator pays opportunity cost. For a sell, a
    negative ``price_change`` means the market dropped before the
    sell completed — also a loss.

    Caller signs ``price_change`` so that a positive value always
    represents a *cost* (i.e. for a buy pass ``new_price - old_price``;
    for a sell pass ``old_price - new_price``).

    Returns USD; rounds to cent. Negative ``price_change`` produces a
    negative cost (favourable drift offsets the implementation
    shortfall in the operator's reconciliation).
    """
    if unfilled_shares < 0:
        raise ValueError("unfilled_shares must be non-negative")
    return (Decimal(unfilled_shares) * price_change).quantize(_TWOPLACES)


__all__ = [
    "DEFAULT_MAKER_REBATE_PER_SHARE",
    "DEFAULT_TAKER_FEE_PER_SHARE",
    "NSCC_FEE_PER_SIDE_2025",
    "exchange_maker_rebate",
    "exchange_taker_fee",
    "half_spread_cost",
    "nscc_clearing_fee",
    "opportunity_cost",
]
