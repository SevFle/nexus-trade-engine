"""Trade-level performance statistics (gh#97 follow-up).

Pure-function helpers operating on a sequence of trade PnLs (signed
floats: ``> 0`` = win, ``< 0`` = loss, ``0`` = breakeven). Complements
the class-bound private methods in
``engine.core.metrics.PerformanceMetrics`` and adds metrics that
weren't previously exposed.

Coverage (numbers from gh#97 taxonomy):

- 44  Max consecutive wins    — ``max_consecutive_wins``
- 45  Max consecutive losses  — ``max_consecutive_losses``
- 46  Current streak          — ``current_streak``
- 47  Profit factor           — ``profit_factor``
- 48  Average win / loss      — ``average_win``, ``average_loss``
- 49  Win/loss ratio          — ``win_loss_ratio``
- 50  Hit ratio               — ``hit_ratio``
- 55  Largest single trade    — ``largest_win``, ``largest_loss``

Conventions:

- Breakeven trades (``pnl == 0``) are *not* counted as wins or losses.
- Empty input returns ``0.0`` (or ``None`` for ``profit_factor`` when
  no losses exist — distinguishing "infinite ratio" from "no data").
- Streaks are reported as positive ints; ``current_streak`` returns
  signed (positive for win streak, negative for loss).

Out of scope:
- Per-symbol rollups (caller groups themselves).
- Win/loss expectancy (already shipped in ``engine.core.metrics_extras``).
- MAE / MFE — separate slice (intra-trade excursion data needed).
"""

from __future__ import annotations

from collections.abc import Sequence


def hit_ratio(trade_pnls: Sequence[float]) -> float:
    """Fraction of trades with strictly-positive PnL.

    Returns ``0.0`` for empty input. Breakeven trades are *not* counted
    as wins (consistent convention with ``compute_positive_period_pct``
    in ``engine.core.time_metrics``).
    """
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)


def average_win(trade_pnls: Sequence[float]) -> float:
    """Mean of strictly-positive trade PnLs. ``0.0`` if no wins."""
    wins = [p for p in trade_pnls if p > 0]
    if not wins:
        return 0.0
    return sum(wins) / len(wins)


def average_loss(trade_pnls: Sequence[float]) -> float:
    """Mean of strictly-negative trade PnLs (returned as a negative number).

    ``0.0`` if no losses.
    """
    losses = [p for p in trade_pnls if p < 0]
    if not losses:
        return 0.0
    return sum(losses) / len(losses)


def win_loss_ratio(trade_pnls: Sequence[float]) -> float:
    """Average win / |average loss|.

    Returns ``0.0`` when there are no losses (caller distinguishes
    "no losses, all wins" via ``hit_ratio`` and ``average_win``).
    Returns ``0.0`` when there are no wins.
    """
    avg_w = average_win(trade_pnls)
    avg_l = average_loss(trade_pnls)
    if avg_w == 0.0 or avg_l == 0.0:
        return 0.0
    return avg_w / abs(avg_l)


def profit_factor(trade_pnls: Sequence[float]) -> float | None:
    """Gross profit / gross loss.

    Returns ``None`` when there are no losing trades (mathematically
    infinite — caller decides how to display). Returns ``0.0`` for
    empty input or no winning trades.
    """
    if not trade_pnls:
        return 0.0
    gross_profit = sum(p for p in trade_pnls if p > 0)
    gross_loss = sum(-p for p in trade_pnls if p < 0)
    if gross_loss == 0.0:
        return None if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def largest_win(trade_pnls: Sequence[float]) -> float:
    """Largest single winning trade. ``0.0`` if no wins."""
    wins = [p for p in trade_pnls if p > 0]
    if not wins:
        return 0.0
    return max(wins)


def largest_loss(trade_pnls: Sequence[float]) -> float:
    """Largest single losing trade (returned negative). ``0.0`` if no losses."""
    losses = [p for p in trade_pnls if p < 0]
    if not losses:
        return 0.0
    return min(losses)


def max_consecutive_wins(trade_pnls: Sequence[float]) -> int:
    """Longest run of strictly-positive trades. Breakeven breaks the streak."""
    longest = 0
    current = 0
    for p in trade_pnls:
        if p > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def max_consecutive_losses(trade_pnls: Sequence[float]) -> int:
    """Longest run of strictly-negative trades. Breakeven breaks the streak."""
    longest = 0
    current = 0
    for p in trade_pnls:
        if p < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def current_streak(trade_pnls: Sequence[float]) -> int:
    """Signed streak length at the end of the sequence.

    Positive = winning streak, negative = losing streak, ``0`` =
    last trade was breakeven (or empty input).
    """
    if not trade_pnls:
        return 0
    last = trade_pnls[-1]
    if last == 0:
        return 0
    sign = 1 if last > 0 else -1
    streak = 0
    for p in reversed(trade_pnls):
        if (sign == 1 and p > 0) or (sign == -1 and p < 0):
            streak += 1
        else:
            break
    return streak * sign


__all__ = [
    "average_loss",
    "average_win",
    "current_streak",
    "hit_ratio",
    "largest_loss",
    "largest_win",
    "max_consecutive_losses",
    "max_consecutive_wins",
    "profit_factor",
    "win_loss_ratio",
]
