"""Time-based analysis section of the performance report (gh#97).

Pydantic model for the 10 time-series / chart datasets (taxonomy items
77-86). Each field maps 1:1 to a chart the analytics dashboard renders:

- ``monthly_heatmap``      — 77  years × months returns grid
- ``day_of_week_returns``  — 78  Mon-Sun average returns
- ``hour_of_day_returns``  — 79  intraday average returns
- ``rolling_sharpe``       — 80  30d / 90d rolling Sharpe
- ``rolling_max_drawdown`` — 81  rolling max drawdown timeseries
- ``equity_curve`` / ``benchmark_curve`` — 82  dual equity series
- ``drawdown_curve``       — 83  underwater series
- ``trade_pnl_distribution`` — 84  trade-PnL histogram bins
- ``daily_return_distribution`` — 85  daily-return histogram + normal overlay
- ``rolling_correlation_to_benchmark`` — 86  rolling correlation
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from engine.core.analytics.returns import PeriodReturn


class TimeAnalysis(BaseModel):
    """Time-based chart datasets — taxonomy items 77-86."""

    monthly_heatmap: dict[str, dict[str, float]] = Field(default_factory=dict)
    day_of_week_returns: list[PeriodReturn] = Field(default_factory=list)
    hour_of_day_returns: list[PeriodReturn] = Field(default_factory=list)
    rolling_sharpe: dict[str, list[float | None]] = Field(default_factory=dict)
    rolling_sortino: dict[str, list[float | None]] = Field(default_factory=dict)
    rolling_max_drawdown: list[float | None] = Field(default_factory=list)
    equity_curve: list[float] = Field(default_factory=list)
    benchmark_curve: list[float] = Field(default_factory=list)
    drawdown_curve: list[float] = Field(default_factory=list)
    trade_pnl_distribution: list[dict[str, Any]] = Field(default_factory=list)
    daily_return_distribution: dict[str, Any] = Field(default_factory=dict)
    rolling_correlation_to_benchmark: list[float | None] = Field(default_factory=list)


__all__ = ["TimeAnalysis"]
