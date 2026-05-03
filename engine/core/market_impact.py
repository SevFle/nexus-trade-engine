"""Almgren-Chriss square-root market-impact model (gh#96 follow-up).

The institutional standard for estimating execution cost when an
order is large relative to the asset's average daily volume. Models
the price drift caused by the trade itself, decomposed into:

- *Temporary impact* — the price movement that reverts after the
  execution finishes (related to liquidity provision spreads).
- *Permanent impact* — information leakage / supply-demand shift
  that does not revert.

Core formula (one-way trade, single instrument)::

    impact = η * σ * sqrt(Q / (V * T))

Where:

- ``η`` (eta) — the dimensionless impact coefficient. Empirical
  literature places it in the range 0.1–0.5 for liquid US equities.
  We default to 0.314 (Almgren et al., 2005, "Direct Estimation of
  Equity Market Impact", Risk Magazine).
- ``σ`` (sigma) — the asset's *daily* volatility, expressed as a
  fraction (e.g. 0.02 for a 2 % daily move).
- ``Q`` — the absolute trade quantity (shares).
- ``V`` — the asset's average daily volume (shares).
- ``T`` — the execution horizon in trading days. Use ``1`` for a
  full-day VWAP execution, ``0.5`` for a half-day, etc.

The function returns the impact as a *price fraction* (e.g. 0.0015
for 15 basis points). Callers multiply by the reference price to
turn it into a dollar amount per share.

Out of scope (explicit follow-ups)
----------------------------------
- Multi-asset portfolio impact + cross-impact terms.
- Time-varying η (intraday seasonality).
- Risk-aversion-driven optimal execution scheduling (the full
  Almgren-Chriss optimisation, not just the impact estimate).
- Limit-order-book microstructure effects (queue position, opp.
  cost of resting).
"""

from __future__ import annotations

import math

# Almgren et al. (2005) baseline. Operators override per asset class
# / liquidity bucket via the ``eta`` kwarg.
DEFAULT_ETA: float = 0.314

# Permanent impact as a fraction of temporary impact. Empirical range
# 0.1–0.3; we pin 0.2 as the midpoint for the default.
DEFAULT_PERMANENT_FRACTION: float = 0.2


def compute_temporary_impact(
    quantity: float,
    daily_volume: float,
    daily_volatility: float,
    *,
    eta: float = DEFAULT_ETA,
    horizon_days: float = 1.0,
) -> float:
    """Square-root temporary-impact estimate as a *price fraction*.

    Returns ``0.0`` when ``quantity``, ``daily_volume``, or
    ``daily_volatility`` is zero. Negative inputs raise.

    The formula assumes the execution drains a fraction
    ``quantity / (daily_volume * horizon_days)`` of the available
    liquidity over ``horizon_days`` days; the impact scales with the
    square root of that fraction.
    """
    _check_non_negative("quantity", quantity)
    _check_non_negative("daily_volume", daily_volume)
    _check_non_negative("daily_volatility", daily_volatility)
    _check_non_negative("eta", eta)
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if quantity == 0 or daily_volume == 0 or daily_volatility == 0:
        return 0.0
    participation = quantity / (daily_volume * horizon_days)
    return eta * daily_volatility * math.sqrt(participation)


def compute_permanent_impact(
    temporary_impact: float,
    *,
    permanent_fraction: float = DEFAULT_PERMANENT_FRACTION,
) -> float:
    """Permanent component as a fixed fraction of temporary impact.

    The Almgren-Chriss decomposition treats permanent impact as a
    constant fraction of the total — typically 10–30 %. Operators
    calibrate per asset class.
    """
    _check_non_negative("temporary_impact", temporary_impact)
    _check_non_negative("permanent_fraction", permanent_fraction)
    return temporary_impact * permanent_fraction


def compute_total_market_impact(
    quantity: float,
    daily_volume: float,
    daily_volatility: float,
    *,
    eta: float = DEFAULT_ETA,
    horizon_days: float = 1.0,
    permanent_fraction: float = DEFAULT_PERMANENT_FRACTION,
) -> tuple[float, float, float]:
    """Return ``(temporary, permanent, total)`` impact fractions.

    Convenience wrapper that runs both halves of the decomposition in
    one call. Operators dollar-cost the result by multiplying each
    fraction by the reference price.
    """
    temp = compute_temporary_impact(
        quantity,
        daily_volume,
        daily_volatility,
        eta=eta,
        horizon_days=horizon_days,
    )
    perm = compute_permanent_impact(
        temp, permanent_fraction=permanent_fraction
    )
    return temp, perm, temp + perm


def _check_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


__all__ = [
    "DEFAULT_ETA",
    "DEFAULT_PERMANENT_FRACTION",
    "compute_permanent_impact",
    "compute_temporary_impact",
    "compute_total_market_impact",
]
