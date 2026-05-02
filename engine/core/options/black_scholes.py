"""Closed-form Black-Scholes pricer + Greeks (gh#83).

References
----------
- Hull, *Options, Futures, and Other Derivatives* (10th ed.), ch. 15
  (Black-Scholes-Merton model) and ch. 19 (Greeks).
- Continuous dividend yield ``q`` follows Merton's extension; set
  ``q=0`` for a non-dividend-paying underlying.

All functions are deterministic and pure-Python — no numpy required.
``math.erf`` provides the cumulative standard-normal needed for both
pricing and Greeks.

Conventions
-----------
- ``S``  : underlying spot price.
- ``K``  : strike.
- ``T``  : time to expiration in *years*.
- ``r``  : continuously compounded risk-free rate (annual).
- ``q``  : continuously compounded dividend yield (annual). Default 0.
- ``sigma``: annualised volatility, expressed as a decimal (e.g. 0.25 = 25%).

Edge cases
----------
- ``T == 0``  : intrinsic value, zero gamma/vega/theta/rho.
- ``sigma <= 0`` raises — there is no Black-Scholes price for it.
- Negative inputs (``S``, ``K``, ``T`` < 0) raise.

Greeks return a :class:`Greeks` dataclass; ``theta`` is reported per
*year*. Many trading platforms quote ``theta`` per *day* — divide by
365 (or 252 for a trading-day calendar) at the boundary if you want
that convention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

# Maximum iterations + convergence tolerance for the implied-vol solver.
_IV_MAX_ITER: int = 100
_IV_TOL: float = 1e-8

# Numerical-stability bounds.
_SIGMA_MIN: float = 1e-9
_VOL_LO: float = 1e-4
_VOL_HI: float = 5.0  # 500% annualised — enough head room for crisis vol


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Greeks:
    """First-order Greeks. ``theta`` is per-year (see module docs)."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf``."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _validate(S: float, K: float, T: float, sigma: float | None) -> None:
    if S < 0:
        raise ValueError("S must be non-negative")
    if K < 0:
        raise ValueError("K must be non-negative")
    if T < 0:
        raise ValueError("T must be non-negative")
    if sigma is not None and sigma <= 0:
        raise ValueError("sigma must be positive")


def _d1_d2(
    S: float, K: float, T: float, r: float, sigma: float, q: float
) -> tuple[float, float]:
    """Black-Scholes ``d1`` and ``d2``. Caller has validated inputs."""
    sigma_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return d1, d2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bs_price(
    *,
    option_type: OptionType,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float = 0.0,
) -> float:
    """Closed-form European option price."""
    _validate(S, K, T, sigma)
    if T == 0:
        # At expiration: intrinsic value.
        intrinsic = (
            max(S - K, 0.0) if option_type == OptionType.CALL else max(K - S, 0.0)
        )
        return intrinsic
    if S == 0:
        # Underlying worthless: call worth 0; put worth K * exp(-rT).
        if option_type == OptionType.CALL:
            return 0.0
        return K * math.exp(-r * T)

    d1, d2 = _d1_d2(S, K, T, r, max(sigma, _SIGMA_MIN), q)
    if option_type == OptionType.CALL:
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


def bs_greeks(
    *,
    option_type: OptionType,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float = 0.0,
) -> Greeks:
    """First-order Greeks (delta, gamma, vega, theta, rho).

    ``theta`` is per *year*; ``vega`` is per 1.0 of vol (i.e. dPrice/dsigma,
    not dPrice/d(percentage point)). Multiply ``vega`` by 0.01 to get the
    common "vega per 1% vol move" form.
    """
    _validate(S, K, T, sigma)
    if T == 0 or S == 0:
        # Degenerate cases: most Greeks collapse to 0; delta becomes the
        # intrinsic-value indicator for an expiring contract.
        delta = 0.0
        if T == 0 and S != 0:
            if option_type == OptionType.CALL:
                delta = 1.0 if S > K else 0.0
            else:
                delta = -1.0 if S < K else 0.0
        return Greeks(delta=delta, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

    sig = max(sigma, _SIGMA_MIN)
    d1, d2 = _d1_d2(S, K, T, r, sig, q)
    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)

    if option_type == OptionType.CALL:
        delta = disc_q * cdf_d1
        rho = K * T * disc_r * cdf_d2
        theta = (
            -(S * disc_q * pdf_d1 * sig) / (2.0 * math.sqrt(T))
            - r * K * disc_r * cdf_d2
            + q * S * disc_q * cdf_d1
        )
    else:  # PUT
        delta = disc_q * (cdf_d1 - 1.0)
        rho = -K * T * disc_r * _norm_cdf(-d2)
        theta = (
            -(S * disc_q * pdf_d1 * sig) / (2.0 * math.sqrt(T))
            + r * K * disc_r * _norm_cdf(-d2)
            - q * S * disc_q * _norm_cdf(-d1)
        )

    gamma = disc_q * pdf_d1 / (S * sig * math.sqrt(T))
    vega = S * disc_q * pdf_d1 * math.sqrt(T)

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def implied_volatility(
    *,
    option_type: OptionType,
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float = 0.0,
    initial_guess: float = 0.20,
    max_iter: int = _IV_MAX_ITER,
    tol: float = _IV_TOL,
) -> float:
    """Newton-Raphson implied-vol solver.

    Falls back to bisection if the Newton step explodes or oscillates.
    Raises ``ValueError`` if ``market_price`` is below intrinsic value
    (no real implied vol exists).
    """
    if market_price < 0:
        raise ValueError("market_price must be non-negative")
    _validate(S, K, T, sigma=None)

    # Intrinsic / upper-bound sanity.
    intrinsic = (
        max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
        if option_type == OptionType.CALL
        else max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    )
    if market_price < intrinsic - tol:
        raise ValueError(
            f"market_price {market_price} below intrinsic {intrinsic}"
        )

    # Newton-Raphson with a bisection backup.
    sigma = max(initial_guess, _VOL_LO)
    lo, hi = _VOL_LO, _VOL_HI
    last_diff = 0.0
    for _ in range(max_iter):
        price = bs_price(option_type=option_type, S=S, K=K, T=T, r=r, sigma=sigma, q=q)
        diff = price - market_price
        last_diff = diff
        if abs(diff) < tol:
            return sigma
        greeks = bs_greeks(
            option_type=option_type, S=S, K=K, T=T, r=r, sigma=sigma, q=q
        )
        if greeks.vega > 1e-10:
            step = diff / greeks.vega
            new_sigma = sigma - step
            if _VOL_LO < new_sigma < _VOL_HI:
                sigma = new_sigma
                continue
        # Bisection fallback.
        if diff > 0:
            hi = sigma
        else:
            lo = sigma
        sigma = 0.5 * (lo + hi)

    raise ValueError(
        f"implied_volatility: did not converge after {max_iter} iterations "
        f"(last sigma={sigma:.6f}, last diff={last_diff:.6e})"
    )
