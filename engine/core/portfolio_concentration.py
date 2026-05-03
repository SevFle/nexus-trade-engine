"""Concentration + volatility decomposition helpers (gh#89 follow-up).

Pure-function helpers on top of the cross-portfolio aggregator from
#339. Concentration analytics answer "how diversified is this group?":

- Herfindahl-Hirschman Index (HHI) over weights
- Top-N concentration share
- Effective N (1 / HHI) — equivalent number of equally-weighted holdings
- Gini coefficient over weights
- Idiosyncratic / systematic variance decomposition

Conventions:

- Weights are passed as a ``Mapping[str, float]`` of ``key → weight``.
  We sum-to-one internally so callers can pass raw exposures and get
  a comparable index across groups.
- All functions return ``0.0`` for empty input rather than raising —
  consistent with the existing metrics layer (``engine.core.metrics``).

Out of scope:
- Bayesian shrinkage on the weight covariance matrix.
- Turnover-decay weighting for time-varying weights.
- Dollar-Volume-weighted concentration (operators bin themselves).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def _normalise(weights: Mapping[str, float]) -> dict[str, float]:
    """Re-scale weights to sum to one, dropping non-positive entries."""
    positive = {k: w for k, w in weights.items() if w > 0}
    total = sum(positive.values())
    if total <= 0:
        return {}
    return {k: w / total for k, w in positive.items()}


def hhi(weights: Mapping[str, float]) -> float:
    """Herfindahl-Hirschman Index over normalised weights.

    Returns a value in ``[0, 1]``. ``0`` for empty input. ``1`` for a
    fully concentrated single-holding portfolio. Weights are normalised
    to sum-to-one internally; non-positive entries are dropped.
    """
    norm = _normalise(weights)
    if not norm:
        return 0.0
    return sum(w * w for w in norm.values())


def effective_n(weights: Mapping[str, float]) -> float:
    """Effective number of equally-weighted holdings, ``1 / HHI``.

    Returns ``0.0`` for empty input. A perfectly diversified portfolio
    of N equal holdings has ``effective_n = N``.
    """
    h = hhi(weights)
    return 1.0 / h if h > 0 else 0.0


def top_n_share(weights: Mapping[str, float], n: int) -> float:
    """Combined weight of the top ``n`` holdings (after normalisation).

    Returns ``0.0`` for empty input or ``n <= 0``. Caps at ``1.0`` when
    ``n >= len(weights)``.
    """
    if n <= 0:
        return 0.0
    norm = _normalise(weights)
    if not norm:
        return 0.0
    sorted_weights = sorted(norm.values(), reverse=True)
    return sum(sorted_weights[:n])


def gini_coefficient(weights: Mapping[str, float]) -> float:
    """Gini coefficient of the (normalised) weight distribution.

    ``0`` = perfectly equal, approaches ``1`` as concentration grows.
    Uses the standard sorted-pair formula:

        G = (2 · Σᵢ i·xᵢ) / (n · Σ xᵢ) − (n + 1) / n

    where ``x`` is sorted ascending and ``i`` is 1-indexed.
    """
    norm = _normalise(weights)
    if not norm:
        return 0.0
    n = len(norm)
    if n == 1:
        return 0.0
    sorted_w = sorted(norm.values())
    cumulative = sum((i + 1) * w for i, w in enumerate(sorted_w))
    total = sum(sorted_w)
    if total == 0.0:
        return 0.0
    return (2 * cumulative) / (n * total) - (n + 1) / n


def variance_decomposition(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> dict[str, float]:
    """Decompose portfolio variance into systematic and idiosyncratic.

    Uses the standard CAPM-style decomposition:

        var(P) = β² · var(B) + var(ε)

    where β is OLS slope of P on B and ε is the residual. Returns a
    dict with keys ``total_variance``, ``systematic_variance``,
    ``idiosyncratic_variance``, ``beta``, and ``r_squared``.

    Returns all-zero values for series shorter than 2, length mismatch,
    or zero-variance benchmark / portfolio.
    """
    zero = {
        "total_variance": 0.0,
        "systematic_variance": 0.0,
        "idiosyncratic_variance": 0.0,
        "beta": 0.0,
        "r_squared": 0.0,
    }
    n = len(portfolio_returns)
    if n != len(benchmark_returns) or n < 2:
        return zero
    mp = sum(portfolio_returns) / n
    mb = sum(benchmark_returns) / n
    cov = (
        sum((p - mp) * (b - mb) for p, b in zip(portfolio_returns, benchmark_returns))
        / n
    )
    var_b = sum((b - mb) ** 2 for b in benchmark_returns) / n
    var_p = sum((p - mp) ** 2 for p in portfolio_returns) / n
    if var_b == 0.0 or var_p == 0.0:
        return zero
    beta = cov / var_b
    systematic = beta * beta * var_b
    idiosyncratic = max(var_p - systematic, 0.0)
    r_squared = systematic / var_p if var_p > 0 else 0.0
    return {
        "total_variance": var_p,
        "systematic_variance": systematic,
        "idiosyncratic_variance": idiosyncratic,
        "beta": beta,
        "r_squared": r_squared,
    }


__all__ = [
    "effective_n",
    "gini_coefficient",
    "hhi",
    "top_n_share",
    "variance_decomposition",
]
