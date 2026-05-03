"""Additional risk-adjusted performance metrics (gh#97 follow-up).

Companion to :mod:`engine.core.metrics`. Each function here is a
pure helper over a returns / equity-curve / drawdown-curve list so
callers can mix-and-match without instantiating the full
:class:`engine.core.metrics.PerformanceMetrics` aggregator.

Metrics covered (KPI numbers from gh#97):

Risk-adjusted (sequence-level):

- 18  Omega ratio                 — full-distribution gain/loss ratio
- 19  Information ratio           — return-vs-benchmark per tracking-error unit
- 24  Gain-to-pain ratio          — sum(returns) / abs(sum(negative returns))
- 29  Recovery factor             — total return / max drawdown
- 30  Ulcer index                 — RMS of the drawdown curve
- 32  Pain index                  — mean of |drawdown|

Trade-level (PnL-list-driven):

- 39  Expectancy ($)              — win_rate * avg_win - loss_rate * avg_loss
- 40  Expectancy (R-multiple)     — expectancy / avg risk per trade
- 44  Payoff ratio                — avg_winner / abs(avg_loser)
- 45  Kelly criterion             — fraction of capital to risk per trade

Out of scope (explicit follow-ups for the remaining ~76 of 86):
- Treynor / MAR / Sterling / K-Ratio (need beta + benchmark series).
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


def compute_payoff_ratio(trade_pnls: Sequence[float]) -> float:
    """Payoff ratio = avg(winners) / abs(avg(losers)).

    Returns ``0.0`` for empty input or when there are no losers and no
    winners. ``inf`` when there are winners but no losers.
    """
    if not trade_pnls:
        return 0.0
    winners = [p for p in trade_pnls if p > 0]
    losers = [p for p in trade_pnls if p < 0]
    if not winners:
        return 0.0
    if not losers:
        return math.inf
    avg_win = sum(winners) / len(winners)
    avg_loss = abs(sum(losers) / len(losers))
    if avg_loss == 0:
        return math.inf
    return avg_win / avg_loss


def compute_expectancy_dollars(trade_pnls: Sequence[float]) -> float:
    """Per-trade dollar expectancy.

    Defined as ``win_rate * avg_win - loss_rate * avg_loss`` where
    ``avg_loss`` is the *positive* magnitude of the average loser. A
    profitable system has expectancy > 0. Equivalent to the simple
    arithmetic mean of all trade PnLs (the formula is just an
    expanded form of that mean).

    Returns ``0.0`` on empty input.
    """
    if not trade_pnls:
        return 0.0
    return sum(trade_pnls) / len(trade_pnls)


def compute_expectancy_r_multiple(
    trade_pnls: Sequence[float],
    avg_risk_per_trade: float,
) -> float:
    """Expectancy expressed in R-multiples.

    R is the per-trade *risk capital*. Dividing the dollar expectancy
    by R normalises it: an expectancy of 0.5 R means a typical trade
    earns half the amount the trader risked. Returns ``0.0`` on empty
    input or non-positive ``avg_risk_per_trade``.
    """
    if not trade_pnls or avg_risk_per_trade <= 0:
        return 0.0
    return compute_expectancy_dollars(trade_pnls) / avg_risk_per_trade


def compute_kelly_criterion(trade_pnls: Sequence[float]) -> float:
    """Kelly criterion — optimal fraction of capital to risk per trade.

    ``f* = win_rate - loss_rate / payoff_ratio``. Result is the
    *theoretical* optimum that maximises long-run geometric growth;
    operators typically apply a half-Kelly or quarter-Kelly haircut
    to absorb estimation error.

    Returns ``0.0`` on empty input, when there are no losers (Kelly is
    not defined for never-losing systems and ``inf`` capital is not a
    useful answer), or when the payoff ratio cannot be formed.
    Negative results are *not* clamped — a negative Kelly means the
    caller should not take the trade.
    """
    if not trade_pnls:
        return 0.0
    winners = [p for p in trade_pnls if p > 0]
    losers = [p for p in trade_pnls if p < 0]
    if not winners or not losers:
        return 0.0
    n = len(trade_pnls)
    win_rate = len(winners) / n
    loss_rate = len(losers) / n
    avg_win = sum(winners) / len(winners)
    avg_loss = abs(sum(losers) / len(losers))
    if avg_loss == 0:
        return 0.0
    payoff = avg_win / avg_loss
    if payoff == 0:
        return 0.0
    return win_rate - loss_rate / payoff


__all__ = [
    "compute_expectancy_dollars",
    "compute_expectancy_r_multiple",
    "compute_gain_to_pain_ratio",
    "compute_information_ratio",
    "compute_kelly_criterion",
    "compute_omega_ratio",
    "compute_pain_index",
    "compute_payoff_ratio",
    "compute_recovery_factor",
    "compute_ulcer_index",
]
