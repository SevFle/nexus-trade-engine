"""Alpha / beta / capture ratio helpers (gh#97 follow-up).

Pure-function helpers for CAPM-style benchmark-relative metrics.
Complements the variance decomposition in
``engine.core.portfolio_concentration`` (#341) and the active-return
streams in ``engine.core.cumulative_returns`` (#349) by adding the
single-number alpha / beta / capture-ratio summaries that benchmark
comparison reports surface.

Coverage (numbers from gh#97 taxonomy):

- 36  Alpha (Jensen)               — ``jensen_alpha``
- 37  Beta                         — ``beta``
- 75  Up-market capture ratio      — ``up_capture_ratio``
- 76  Down-market capture ratio    — ``down_capture_ratio``
- 77  Capture ratio (up/down)      — ``capture_ratio``

Conventions:

- All inputs are *period* returns (e.g. daily). The caller passes an
  annualisation factor (default 252) so we can produce annualised
  alpha. Beta is unit-less.
- Length-mismatched inputs raise ``ValueError``.
- Empty inputs / fewer-than-2 points / zero-variance benchmark return
  ``0.0``.

Out of scope:
- Treynor-Mazuy / Henriksson-Merton timing alpha.
- Multi-factor (Fama-French) decomposition — separate slice.
- Rolling alpha / beta — analogous slice when needed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def beta(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """OLS slope of portfolio on benchmark returns.

    Returns ``0.0`` when fewer than 2 points, length mismatch, or
    benchmark has zero variance. A market-neutral portfolio returns
    ``0.0``; an inverse-correlated portfolio returns negative.
    """
    if len(portfolio_returns) != len(benchmark_returns):
        raise ValueError(
            f"length mismatch: {len(portfolio_returns)} "
            f"vs {len(benchmark_returns)}"
        )
    n = len(portfolio_returns)
    if n < 2:
        return 0.0
    mp = _mean(portfolio_returns)
    mb = _mean(benchmark_returns)
    cov = (
        sum((p - mp) * (b - mb) for p, b in zip(portfolio_returns, benchmark_returns))
        / n
    )
    var_b = sum((b - mb) ** 2 for b in benchmark_returns) / n
    # Guard against float-precision residuals from constant-input series
    # (e.g. mean-subtracted [0.05, 0.05, 0.05] leaves ~1e-17 bits).
    if var_b < 1e-20:
        return 0.0
    return cov / var_b


def jensen_alpha(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    risk_free_rate: float = 0.0,
    annualisation_factor: int = 252,
) -> float:
    """Annualised Jensen's alpha.

    ``α = (E[Rp] - Rf) - β · (E[Rb] - Rf)`` in per-period units, then
    multiplied by ``annualisation_factor``. Returns ``0.0`` for empty /
    too-short inputs.
    """
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    if len(portfolio_returns) != len(benchmark_returns):
        raise ValueError(
            f"length mismatch: {len(portfolio_returns)} "
            f"vs {len(benchmark_returns)}"
        )
    n = len(portfolio_returns)
    if n < 2:
        return 0.0
    rf_period = risk_free_rate / annualisation_factor
    b = beta(portfolio_returns, benchmark_returns)
    excess_p = _mean(portfolio_returns) - rf_period
    excess_b = _mean(benchmark_returns) - rf_period
    return (excess_p - b * excess_b) * annualisation_factor


def _compounded_return(returns: Sequence[float]) -> float:
    """Compounded return: ``∏(1 + r) - 1``."""
    product = 1.0
    for r in returns:
        product *= 1.0 + r
    return product - 1.0


def up_capture_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Compounded portfolio return / compounded benchmark return on up bars.

    Filters to bars where ``benchmark > 0`` and compounds both streams.
    Returns ``0.0`` when no up bars exist or the benchmark up-period
    return is zero. ``> 1`` means the portfolio captured more than 100 %
    of the up move.
    """
    if len(portfolio_returns) != len(benchmark_returns):
        raise ValueError(
            f"length mismatch: {len(portfolio_returns)} "
            f"vs {len(benchmark_returns)}"
        )
    pairs = [
        (p, b)
        for p, b in zip(portfolio_returns, benchmark_returns)
        if b > 0
    ]
    if not pairs:
        return 0.0
    p_compound = _compounded_return([p for p, _ in pairs])
    b_compound = _compounded_return([b for _, b in pairs])
    if b_compound == 0.0:
        return 0.0
    return p_compound / b_compound


def down_capture_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Compounded portfolio return / compounded benchmark return on down bars.

    Filters to bars where ``benchmark < 0``. ``< 1`` means the
    portfolio fell less than the benchmark on down days (a good
    defensive signal); ``> 1`` means it amplified the loss.
    """
    if len(portfolio_returns) != len(benchmark_returns):
        raise ValueError(
            f"length mismatch: {len(portfolio_returns)} "
            f"vs {len(benchmark_returns)}"
        )
    pairs = [
        (p, b)
        for p, b in zip(portfolio_returns, benchmark_returns)
        if b < 0
    ]
    if not pairs:
        return 0.0
    p_compound = _compounded_return([p for p, _ in pairs])
    b_compound = _compounded_return([b for _, b in pairs])
    if b_compound == 0.0:
        return 0.0
    return p_compound / b_compound


def capture_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Up-capture / down-capture.

    A single-number summary: ``> 1`` means asymmetric upside (great);
    ``< 1`` means more downside than upside (bad). Returns ``0.0`` when
    down-capture is zero (mathematically infinite — caller decides
    display).
    """
    up = up_capture_ratio(portfolio_returns, benchmark_returns)
    down = down_capture_ratio(portfolio_returns, benchmark_returns)
    if down == 0.0:
        return 0.0
    return up / down


def correlation(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Pearson correlation of portfolio and benchmark.

    Bounded ``[-1, 1]``. Returns ``0.0`` for empty / single-point /
    length-mismatch / zero-variance inputs.
    """
    if len(portfolio_returns) != len(benchmark_returns):
        raise ValueError(
            f"length mismatch: {len(portfolio_returns)} "
            f"vs {len(benchmark_returns)}"
        )
    n = len(portfolio_returns)
    if n < 2:
        return 0.0
    mp = _mean(portfolio_returns)
    mb = _mean(benchmark_returns)
    num = sum(
        (p - mp) * (b - mb)
        for p, b in zip(portfolio_returns, benchmark_returns)
    )
    dp = math.sqrt(sum((p - mp) ** 2 for p in portfolio_returns))
    db = math.sqrt(sum((b - mb) ** 2 for b in benchmark_returns))
    if dp == 0.0 or db == 0.0:
        return 0.0
    return num / (dp * db)


__all__ = [
    "beta",
    "capture_ratio",
    "correlation",
    "down_capture_ratio",
    "jensen_alpha",
    "up_capture_ratio",
]
