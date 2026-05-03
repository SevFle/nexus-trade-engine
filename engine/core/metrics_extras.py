"""Additional risk-adjusted performance metrics (gh#97 follow-up).

Companion to :mod:`engine.core.metrics`. Each function here is a
pure helper over a returns / equity-curve / drawdown-curve list so
callers can mix-and-match without instantiating the full
:class:`engine.core.metrics.PerformanceMetrics` aggregator.

Six metrics covered in this slice (KPI numbers from gh#97):

- 18  Omega ratio                 — full-distribution gain/loss ratio
- 19  Information ratio           — return-vs-benchmark per tracking-error unit
- 24  Gain-to-pain ratio          — sum(returns) / abs(sum(negative returns))
- 30  Ulcer index                 — RMS of the drawdown curve
- 32  Pain index                  — mean of |drawdown|
- 29  Recovery factor             — total return / max drawdown

Out of scope (explicit follow-ups for the remaining 80 of 86):
- Treynor / MAR / Sterling / K-Ratio (need beta + benchmark series).
- Trade-level breakdowns (Expectancy R-multiple, Kelly, payoff ratio).
- Time-based heatmaps + monthly/weekly distributions.
- Cost / execution / exposure analytics.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def compute_omega_ratio(
    returns: Sequence[float],
    threshold: float = 0.0,
) -> float:
    """Return the Omega ratio at ``threshold``.

    Defined as ``sum(max(r - threshold, 0)) / sum(max(threshold - r, 0))``.
    A value above 1 means the gain-side mass outweighs the loss-side mass
    relative to the threshold; value greater than the bar suggests the
    strategy is acceptable for an investor with that target return.

    Returns ``inf`` when the loss-side mass is zero and there is at least
    one above-threshold return; ``0.0`` when the input is empty.
    """
    if not returns:
        return 0.0
    upside = 0.0
    downside = 0.0
    for r in returns:
        delta = r - threshold
        if delta > 0:
            upside += delta
        else:
            downside += -delta
    if downside == 0:
        return math.inf if upside > 0 else 0.0
    return upside / downside


def compute_information_ratio(
    returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Return the Information Ratio of ``returns`` vs ``benchmark_returns``.

    Defined as ``mean(active_returns) / std(active_returns)`` where
    ``active = returns - benchmark`` (sample stdev, ddof=1). Both
    sequences must be the same length and contain at least 2 points;
    otherwise the function returns ``0.0``.

    A positive IR signals the strategy outperforms the benchmark on a
    risk-adjusted basis; the magnitude tells you how *consistent* the
    outperformance is.
    """
    n = len(returns)
    if n != len(benchmark_returns) or n < 2:
        return 0.0
    active = [r - b for r, b in zip(returns, benchmark_returns, strict=True)]
    mean = sum(active) / n
    var = sum((x - mean) ** 2 for x in active) / (n - 1)
    if var == 0:
        return 0.0
    return mean / math.sqrt(var)


def compute_gain_to_pain_ratio(returns: Sequence[float]) -> float:
    """Sum of returns divided by absolute sum of *negative* returns.

    Returns 0.0 for empty input. ``inf`` when there are no negative
    returns and the sum is positive (the strategy never lost) — caller
    can clamp for display.
    """
    if not returns:
        return 0.0
    total = sum(returns)
    pain = sum(-r for r in returns if r < 0)
    if pain == 0:
        return math.inf if total > 0 else 0.0
    return total / pain


def compute_ulcer_index(equity_curve: Sequence[float]) -> float:
    """Ulcer index — root-mean-square of the percentage drawdown curve.

    Lower is better. Convention: returns the index in *percent* (i.e.
    a 5 % RMS drawdown returns ``5.0``).
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    sq_sum = 0.0
    n = len(equity_curve)
    for v in equity_curve:
        peak = max(peak, v)
        if peak <= 0:
            continue
        dd_pct = ((peak - v) / peak) * 100.0
        sq_sum += dd_pct * dd_pct
    return math.sqrt(sq_sum / n)


def compute_pain_index(drawdown_curve: Sequence[float]) -> float:
    """Pain index — arithmetic mean of |drawdown|.

    Operates on a drawdown sequence where each entry is a fractional
    drawdown (0.0 to 1.0). Returns the mean as a percent for parity
    with :func:`compute_ulcer_index`.
    """
    if not drawdown_curve:
        return 0.0
    return (sum(abs(d) for d in drawdown_curve) / len(drawdown_curve)) * 100.0


def compute_recovery_factor(
    total_return_pct: float,
    max_drawdown_pct: float,
) -> float:
    """Recovery factor — ``total_return / max_drawdown``.

    Both arguments are percentages (e.g. ``25.0`` for 25 %). Returns
    ``0.0`` when ``max_drawdown_pct`` is zero or negative; the metric
    is undefined in that regime.
    """
    if max_drawdown_pct <= 0:
        return 0.0
    return total_return_pct / max_drawdown_pct


__all__ = [
    "compute_gain_to_pain_ratio",
    "compute_information_ratio",
    "compute_omega_ratio",
    "compute_pain_index",
    "compute_recovery_factor",
    "compute_ulcer_index",
]
