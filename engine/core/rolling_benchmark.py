"""Rolling alpha / beta / IR / tracking-error time series (gh#97 follow-up).

Pure-function helpers producing the *full* rolling series of CAPM-style
benchmark-relative metrics. Complements the full-period helpers in
``engine.core.benchmark_comparison`` (#350) and the single-series rolling
metrics in ``engine.core.rolling_metrics`` (#343).

Coverage (numbers from gh#97 taxonomy):

- 56c Rolling alpha            — ``rolling_alpha``
- 56d Rolling beta             — ``rolling_beta``
- 56e Rolling Information Rat. — ``rolling_information_ratio``
- 72b Rolling tracking error   — ``rolling_tracking_error``

Output convention follows ``engine.core.rolling_metrics`` (#343):
same-length output with ``None`` for the first ``window - 1`` indices.

Out of scope:
- Rolling Treynor-Mazuy timing alpha — separate slice.
- Rolling capture ratios — caller can compose via filter+rolling.
- Multi-factor rolling regression.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_ANNUALISATION = 252


def _validate_window(window: int) -> None:
    if window < 2:
        raise ValueError("window must be >= 2")


def _validate_pair(a: Sequence[float], b: Sequence[float]) -> None:
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")


def _beta_window(port: Sequence[float], bench: Sequence[float]) -> float:
    """OLS slope of portfolio on benchmark over one window. ``0.0`` if degenerate."""
    n = len(port)
    if n < 2:
        return 0.0
    mp = sum(port) / n
    mb = sum(bench) / n
    cov = sum((p - mp) * (b - mb) for p, b in zip(port, bench, strict=False)) / n
    var_b = sum((b - mb) ** 2 for b in bench) / n
    if var_b < 1e-20:
        return 0.0
    return cov / var_b


def rolling_beta(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    window: int,
) -> list[float | None]:
    """Rolling OLS slope of portfolio on benchmark.

    ``None`` for the first ``window - 1`` indices. ``0.0`` for windows
    where the benchmark has zero variance.
    """
    _validate_window(window)
    _validate_pair(portfolio_returns, benchmark_returns)
    n = len(portfolio_returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = _beta_window(
            portfolio_returns[i - window + 1 : i + 1],
            benchmark_returns[i - window + 1 : i + 1],
        )
    return out


def rolling_alpha(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    window: int,
    *,
    risk_free_rate: float = 0.0,
    annualisation_factor: int = DEFAULT_ANNUALISATION,
) -> list[float | None]:
    """Rolling annualised Jensen's alpha.

    Per-window: ``α = (E[Rp] - Rf) - β · (E[Rb] - Rf)`` × ``ann``.
    """
    _validate_window(window)
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    _validate_pair(portfolio_returns, benchmark_returns)
    n = len(portfolio_returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    rf_period = risk_free_rate / annualisation_factor
    for i in range(window - 1, n):
        port = portfolio_returns[i - window + 1 : i + 1]
        bench = benchmark_returns[i - window + 1 : i + 1]
        b = _beta_window(port, bench)
        excess_p = sum(port) / window - rf_period
        excess_b = sum(bench) / window - rf_period
        out[i] = (excess_p - b * excess_b) * annualisation_factor
    return out


def _stdev(xs: Sequence[float], *, ddof: int = 1) -> float:
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def rolling_tracking_error(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    window: int,
    *,
    annualisation_factor: int = DEFAULT_ANNUALISATION,
) -> list[float | None]:
    """Rolling annualised stdev of active returns.

    ``TE = stdev(active) × √(annualisation_factor)``. Constant-active
    windows (zero variance) return ``0.0``.
    """
    _validate_window(window)
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    _validate_pair(portfolio_returns, benchmark_returns)
    n = len(portfolio_returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    sqrt_ann = math.sqrt(annualisation_factor)
    for i in range(window - 1, n):
        active = [
            p - b
            for p, b in zip(
                portfolio_returns[i - window + 1 : i + 1],
                benchmark_returns[i - window + 1 : i + 1],
                strict=False,
            )
        ]
        out[i] = _stdev(active) * sqrt_ann
    return out


def rolling_information_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    window: int,
    *,
    annualisation_factor: int = DEFAULT_ANNUALISATION,
) -> list[float | None]:
    """Rolling annualised Information Ratio.

    ``IR = (E[Rp] - E[Rb]) / stdev(active) × √(ann)``. Returns ``0.0``
    for zero-variance active streams (degenerate constant excess).
    """
    _validate_window(window)
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    _validate_pair(portfolio_returns, benchmark_returns)
    n = len(portfolio_returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    sqrt_ann = math.sqrt(annualisation_factor)
    for i in range(window - 1, n):
        port = portfolio_returns[i - window + 1 : i + 1]
        bench = benchmark_returns[i - window + 1 : i + 1]
        active = [p - b for p, b in zip(port, bench, strict=False)]
        sd = _stdev(active)
        if sd == 0.0:
            out[i] = 0.0
        else:
            mean_active = sum(active) / window
            out[i] = mean_active / sd * sqrt_ann
    return out


__all__ = [
    "DEFAULT_ANNUALISATION",
    "rolling_alpha",
    "rolling_beta",
    "rolling_information_ratio",
    "rolling_tracking_error",
]
