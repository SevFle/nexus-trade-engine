"""Return-distribution + tail-risk metrics (gh#97 follow-up).

Pure-function helpers operating on a flat sequence of period returns.
Adds the distributional and tail-risk KPIs that complement the
existing Sharpe / Sortino / drawdown layers.

Coverage (numbers from gh#97 taxonomy):

- 38  Skewness                — ``skewness``
- 39  Kurtosis (excess)       — ``kurtosis``
- 40  Value at Risk (VaR)     — ``value_at_risk_historical``,
                                ``value_at_risk_parametric``
- 41  Conditional VaR / ES    — ``conditional_value_at_risk``
- 42  Tail ratio              — ``tail_ratio``

Conventions:

- VaR / CVaR are returned as *positive* magnitudes representing the
  magnitude of the loss (e.g. ``0.05`` = a 5 % loss). The caller
  decides whether to display them as negative depending on UI.
- Confidence levels are passed as the *upper* bound: ``0.95`` means
  "95 % VaR" (5 % tail). Bounded ``(0.0, 1.0)``.
- Empty input returns ``0.0`` from every helper.

Out of scope:
- Filtered historical simulation (FHS) — separate slice.
- Cornish-Fisher expansion VaR — separate slice.
- Monte Carlo VaR (the existing ``engine.core.monte_carlo`` module
  covers MC simulation; this module sticks to closed-form / empirical).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: Sequence[float], *, ddof: int = 1) -> float:
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def skewness(returns: Sequence[float]) -> float:
    """Sample skewness (Fisher-Pearson, bias-corrected for small n).

    Returns ``0.0`` for empty input, fewer than 3 data points, or a
    zero-variance series.
    """
    n = len(returns)
    if n < 3:  # noqa: PLR2004
        return 0.0
    m = _mean(returns)
    if max(returns) - min(returns) == 0.0:
        return 0.0
    s = _stdev(returns)
    raw = sum((r - m) ** 3 for r in returns) / n
    biased = raw / (s**3)
    correction = math.sqrt(n * (n - 1)) / (n - 2)
    return correction * biased


def kurtosis(returns: Sequence[float]) -> float:
    """Excess kurtosis (kurtosis - 3, the Fisher convention).

    Bias-corrected per Joanes & Gill (1998). Normal distribution → 0;
    fat tails → > 0; thin tails → < 0. Returns ``0.0`` for empty input,
    fewer than 4 data points, or a zero-variance series.
    """
    n = len(returns)
    if n < 4:  # noqa: PLR2004
        return 0.0
    m = _mean(returns)
    s = _stdev(returns)
    if s == 0.0:
        return 0.0
    raw = sum((r - m) ** 4 for r in returns) / n
    biased = raw / (s**4) - 3
    correction = (n - 1) / ((n - 2) * (n - 3))
    return correction * ((n + 1) * biased + 6)


def _validate_confidence(confidence: float) -> None:
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1); got {confidence}")


def value_at_risk_historical(returns: Sequence[float], *, confidence: float = 0.95) -> float:
    """Historical-simulation VaR at the given confidence level.

    Returns the *magnitude* of the loss at the ``(1 - confidence)``
    quantile (positive value). For example ``confidence=0.95`` returns
    the 5th-percentile loss as a positive number.

    Returns ``0.0`` when the cutoff is non-negative (e.g. all returns
    positive). Empty input → ``0.0``.
    """
    if not returns:
        return 0.0
    _validate_confidence(confidence)
    sorted_r = sorted(returns)
    cutoff_idx = int((1 - confidence) * len(sorted_r))
    cutoff_idx = min(cutoff_idx, len(sorted_r) - 1)
    threshold = sorted_r[cutoff_idx]
    return max(-threshold, 0.0)


def value_at_risk_parametric(returns: Sequence[float], *, confidence: float = 0.95) -> float:
    """Parametric VaR assuming Gaussian returns.

    Uses ``VaR = -(μ + z · sigma)`` where z is the standard-normal quantile
    at ``1 - confidence``. Approximates z via inverse-CDF rational
    approximation (Beasley-Springer-Moro), so no scipy dependency.
    Returns ``0.0`` for empty input, fewer than 2 points, or zero
    variance.
    """
    n = len(returns)
    if n < 2:  # noqa: PLR2004
        return 0.0
    _validate_confidence(confidence)
    m = _mean(returns)
    s = _stdev(returns)
    if s == 0.0:
        return 0.0
    z = _inverse_normal_cdf(1 - confidence)
    return max(-(m + z * s), 0.0)


def _inverse_normal_cdf(p: float) -> float:
    """Inverse of the standard-normal CDF via Beasley-Springer-Moro.

    Accurate to about 1e-9 across the unit interval.
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"p must be in (0, 1); got {p}")
    a = (
        -3.969683028665376e1,
        2.209460984245205e2,
        -2.759285104469687e2,
        1.383577518672690e2,
        -3.066479806614716e1,
        2.506628277459239,
    )
    b = (
        -5.447609879822406e1,
        1.615858368580409e2,
        -1.556989798598866e2,
        6.680131188771972e1,
        -1.328068155288572e1,
    )
    c = (
        -7.784894002430293e-3,
        -3.223964580411365e-1,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (
        7.784695709041462e-3,
        3.224671290700398e-1,
        2.445134137142996,
        3.754408661907416,
    )
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
    )


def conditional_value_at_risk(returns: Sequence[float], *, confidence: float = 0.95) -> float:
    """Conditional VaR / Expected Shortfall (historical).

    Mean of returns at or below the ``(1 - confidence)`` quantile, as a
    positive magnitude. Empty input or non-negative tail → ``0.0``.
    """
    if not returns:
        return 0.0
    _validate_confidence(confidence)
    sorted_r = sorted(returns)
    cutoff_idx = int((1 - confidence) * len(sorted_r))
    if cutoff_idx == 0:
        return max(-sorted_r[0], 0.0)
    tail = sorted_r[:cutoff_idx]
    if not tail:
        return 0.0
    avg_loss = sum(tail) / len(tail)
    return max(-avg_loss, 0.0)


def tail_ratio(returns: Sequence[float], *, percentile: float = 0.95) -> float:
    """Right-tail / left-tail magnitude ratio.

    ``ratio = |percentile-th return| / |(1-percentile)-th return|``.

    A value > 1 means upside tail bigger than downside tail. Useful
    for quickly spotting positive-skew strategies. Returns ``0.0`` for
    empty input or when the left-tail magnitude is zero.
    """
    if not returns:
        return 0.0
    if not 0.5 < percentile < 1.0:  # noqa: PLR2004
        raise ValueError(f"percentile must be in (0.5, 1.0); got {percentile}")
    sorted_r = sorted(returns)
    n = len(sorted_r)
    upper_idx = min(int(percentile * n), n - 1)
    lower_idx = max(int((1 - percentile) * n), 0)
    upper = abs(sorted_r[upper_idx])
    lower = abs(sorted_r[lower_idx])
    if lower == 0.0:
        return 0.0
    return upper / lower


__all__ = [
    "conditional_value_at_risk",
    "kurtosis",
    "skewness",
    "tail_ratio",
    "value_at_risk_historical",
    "value_at_risk_parametric",
]
