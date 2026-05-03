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

Benchmark / regression (return + equity curve):

- 20  Treynor ratio               — excess return / beta
- 21  MAR ratio                   — CAGR / max drawdown (full period)
- 22  Sterling ratio              — CAGR / (avg drawdown - 10 %)
- 23  K-Ratio                     — regression slope of log equity / std error

Out of scope (explicit follow-ups for the remaining ~72 of 86):
- Time-based heatmaps + monthly/weekly distributions.
- Cost / execution / exposure analytics.
- Beta estimation against a benchmark return series (caller supplies
  beta to ``compute_treynor_ratio``).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    if n != len(benchmark_returns) or n < 2:  # noqa: PLR2004
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


def compute_treynor_ratio(
    portfolio_return: float,
    risk_free_rate: float,
    beta: float,
) -> float:
    """Treynor ratio — excess return per unit of *systematic* risk.

    ``(R_p - R_f) / beta``. Where Sharpe normalises by total risk
    (volatility), Treynor only penalises systematic (market-correlated)
    risk; idiosyncratic risk diversifies away in a CAPM world.

    Returns ``0.0`` when ``beta`` is zero (the portfolio carries no
    systematic risk by construction).

    All three inputs are floats expressed as fractions (e.g. 0.12 for
    12 % annual return). Caller is responsible for estimating beta
    upstream — the helper is portfolio-statistics-only.
    """
    if beta == 0:
        return 0.0
    return (portfolio_return - risk_free_rate) / beta


def compute_mar_ratio(
    cagr_pct: float,
    max_drawdown_pct: float,
) -> float:
    """MAR ratio — ``CAGR / max_drawdown`` over the full track record.

    Equivalent to Calmar at the full-period horizon (Calmar is
    typically the trailing 36-month version). Higher is better.
    Returns ``0.0`` when ``max_drawdown_pct`` is zero or negative.

    Both inputs are percentages (e.g. ``25.0`` for 25 %).
    """
    if max_drawdown_pct <= 0:
        return 0.0
    return cagr_pct / max_drawdown_pct


def compute_sterling_ratio(
    cagr_pct: float,
    avg_drawdown_pct: float,
    *,
    drawdown_floor_pct: float = 10.0,
) -> float:
    """Sterling ratio — ``CAGR / (avg_drawdown - floor)``.

    The ``drawdown_floor_pct`` (default 10 %) is the assumed baseline
    drawdown a strategy *should* tolerate; the ratio compounds the
    penalty when the strategy's average drawdown exceeds it. Operators
    override the floor for less-volatile asset classes.

    Returns ``0.0`` when the denominator is zero or negative.
    """
    denom = avg_drawdown_pct - drawdown_floor_pct
    if denom <= 0:
        return 0.0
    return cagr_pct / denom


def compute_k_ratio(equity_curve: Sequence[float]) -> float:
    """K-Ratio — regression-based smoothness measure.

    Defined as the slope of the OLS regression line of the *log* equity
    curve against time, divided by its standard error and scaled by
    ``sqrt(n)`` where ``n`` is the number of observations.

    Higher is better — a smooth log-equity curve produces a high
    slope/error ratio. The metric is sensitive to compounding rate
    *and* path consistency.

    Returns ``0.0`` for fewer than 2 observations, when any equity
    value is non-positive (log undefined), or when the regression
    has zero variance in time (degenerate).
    """
    n = len(equity_curve)
    if n < 2:  # noqa: PLR2004
        return 0.0
    if any(v <= 0 for v in equity_curve):
        return 0.0

    xs = list(range(n))
    ys = [math.log(v) for v in equity_curve]

    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - x_mean) ** 2 for x in xs)

    if sxx == 0:
        return 0.0

    slope = sxy / sxx
    intercept = y_mean - slope * x_mean

    # Residual sum of squares; standard error of the slope.
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    sse = sum(r * r for r in residuals)
    if n <= 2:  # noqa: PLR2004
        return 0.0
    se = math.sqrt(sse / (n - 2)) / math.sqrt(sxx)
    if se == 0:
        return 0.0
    return slope / se * math.sqrt(n)


__all__ = [
    "compute_expectancy_dollars",
    "compute_expectancy_r_multiple",
    "compute_gain_to_pain_ratio",
    "compute_information_ratio",
    "compute_k_ratio",
    "compute_kelly_criterion",
    "compute_mar_ratio",
    "compute_omega_ratio",
    "compute_pain_index",
    "compute_payoff_ratio",
    "compute_recovery_factor",
    "compute_sterling_ratio",
    "compute_treynor_ratio",
    "compute_ulcer_index",
]
