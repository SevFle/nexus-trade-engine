"""Options pricing and analytics (gh#83).

Today this exposes the closed-form Black-Scholes pricer for European
calls and puts, the standard first-order Greeks (delta, gamma, vega,
theta, rho), and a Newton-Raphson implied-volatility solver.

Out of scope (explicit follow-ups):
- Options-chain ingestion from broker / data feeds (gh#104).
- IV surface fitting + skew analytics.
- American-style early exercise (binomial / PDE pricing).
- Dividends beyond a flat continuous yield.
"""

from engine.core.options.black_scholes import (
    Greeks,
    OptionType,
    bs_greeks,
    bs_price,
    implied_volatility,
)

__all__ = [
    "Greeks",
    "OptionType",
    "bs_greeks",
    "bs_price",
    "implied_volatility",
]
