"""Cumulative return + return-stream comparison helpers (gh#97 follow-up).

Pure-function helpers operating on flat sequences of period returns
(or equity curves). Provides the per-bar compounded series, log-return
conversion, equity reconstruction, and pairwise comparison primitives
that the chart layer needs without instantiating a full report.

Coverage (numbers from gh#97 taxonomy):

- 1   Total cumulative return       — ``cumulative_returns`` last bar
- 2   Cumulative return series      — ``cumulative_returns``
- 3   Equity from returns           — ``equity_curve_from_returns``
- 70  Log returns                   — ``log_returns``
- 71  Active return series          — ``active_returns``
- 72  Tracking-error stream         — ``tracking_error``
- 73  Beating-benchmark hit rate    — ``beating_benchmark_pct``

Conventions:

- ``cumulative_returns(returns)`` returns a list the same length as
  the input. Each element is the compounded return up to and including
  that bar (so the first bar equals ``returns[0]``).
- All helpers return ``[]`` or ``0.0`` for empty input rather than
  raising.
- Length-mismatched comparison helpers raise ``ValueError``.

Out of scope:
- Calendar-period rollups (already in ``engine.core.time_metrics``).
- Rolling-window metrics (already in ``engine.core.rolling_metrics``).
- Drawdown analytics (already in ``engine.core.drawdown_analytics``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def cumulative_returns(returns: Sequence[float]) -> list[float]:
    """Per-bar compounded cumulative return.

    ``cumulative[i] = ∏(1 + r_j) - 1`` for ``j ≤ i``. Empty input → ``[]``.
    """
    if not returns:
        return []
    out: list[float] = []
    product = 1.0
    for r in returns:
        product *= 1.0 + r
        out.append(product - 1.0)
    return out


def equity_curve_from_returns(
    returns: Sequence[float], *, initial_value: float = 1.0
) -> list[float]:
    """Reconstruct equity curve from period returns.

    Returns a list of length ``len(returns) + 1``. The first element
    is ``initial_value``; subsequent bars compound forward. Empty
    returns → ``[initial_value]``.
    """
    if initial_value <= 0:
        raise ValueError(f"initial_value must be > 0; got {initial_value}")
    out = [initial_value]
    for r in returns:
        out.append(out[-1] * (1.0 + r))
    return out


def log_returns(returns: Sequence[float]) -> list[float]:
    """Convert simple returns to log returns via ``ln(1 + r)``.

    Returns matching length. Inputs ``r <= -1`` raise ``ValueError``
    (would imply equity went to ≤ 0; log undefined).
    """
    out: list[float] = []
    for r in returns:
        if r <= -1.0:
            raise ValueError(
                f"return must be > -1 (equity > 0); got {r}"
            )
        out.append(math.log(1.0 + r))
    return out


def returns_from_equity(equity: Sequence[float]) -> list[float]:
    """Compute per-bar simple returns from an equity curve.

    Returns a list of length ``len(equity) - 1``. Empty / single-point
    input → ``[]``. Non-positive previous bars yield ``0.0`` (avoids
    divide-by-zero) so this works on stub data.
    """
    n = len(equity)
    if n < 2:
        return []
    out: list[float] = []
    for i in range(1, n):
        prev = equity[i - 1]
        if prev <= 0:
            out.append(0.0)
        else:
            out.append((equity[i] - prev) / prev)
    return out


def active_returns(
    portfolio: Sequence[float], benchmark: Sequence[float]
) -> list[float]:
    """Per-bar excess return over a benchmark.

    Both inputs must have identical length; mismatch raises
    ``ValueError``. Empty inputs → ``[]``.
    """
    if len(portfolio) != len(benchmark):
        raise ValueError(
            f"length mismatch: {len(portfolio)} vs {len(benchmark)}"
        )
    return [p - b for p, b in zip(portfolio, benchmark)]


def tracking_error(
    portfolio: Sequence[float],
    benchmark: Sequence[float],
    *,
    annualisation_factor: int = 252,
) -> float:
    """Annualised standard deviation of active returns.

    ``TE = stdev(active) × √(annualisation_factor)``. Returns ``0.0``
    for fewer than 2 points. ``annualisation_factor <= 0`` rejected.
    """
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    active = active_returns(portfolio, benchmark)
    n = len(active)
    if n < 2:
        return 0.0
    m = sum(active) / n
    var = sum((x - m) ** 2 for x in active) / (n - 1)
    return math.sqrt(var) * math.sqrt(annualisation_factor)


def beating_benchmark_pct(
    portfolio: Sequence[float], benchmark: Sequence[float]
) -> float:
    """Fraction of bars where portfolio strictly beats benchmark.

    Empty input → ``0.0``. Length mismatch → ``ValueError``. Ties
    (``p == b``) are *not* counted as beating.
    """
    if len(portfolio) != len(benchmark):
        raise ValueError(
            f"length mismatch: {len(portfolio)} vs {len(benchmark)}"
        )
    if not portfolio:
        return 0.0
    wins = sum(1 for p, b in zip(portfolio, benchmark) if p > b)
    return wins / len(portfolio)


__all__ = [
    "active_returns",
    "beating_benchmark_pct",
    "cumulative_returns",
    "equity_curve_from_returns",
    "log_returns",
    "returns_from_equity",
    "tracking_error",
]
