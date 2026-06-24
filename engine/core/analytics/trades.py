"""Trade-level section of the performance report (gh#97).

Pydantic model for the 16 trade KPIs (taxonomy items 35-50). Trade
count, win/loss rates, profit factor, expectancy, payoff, streaks,
holding periods and the trades-per-period cadence. ``realized_pnl`` is
extracted from the trade log by the analyzer; breakeven trades are
treated as neither wins nor losses (matches
:mod:`engine.core.trade_stats`).
"""

from __future__ import annotations

from pydantic import BaseModel


class TradeMetrics(BaseModel):
    """Trade-level KPIs — taxonomy items 35-50."""

    total_trades: int = 0
    win_rate_pct: float = 0.0
    loss_rate_pct: float = 0.0
    profit_factor: float | None = None
    expectancy_dollars: float = 0.0
    expectancy_r_multiple: float | None = None
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    payoff_ratio: float | None = None
    largest_winner: float = 0.0
    largest_loser: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_period_hours: float = 0.0
    median_holding_period_hours: float = 0.0
    trades_per_day: float = 0.0
    trades_per_week: float = 0.0
    trades_per_month: float = 0.0


__all__ = ["TradeMetrics"]
