"""Return-metrics section of the performance report (gh#97).

Pydantic model for the 14 return KPIs (taxonomy items 1-14) plus the
small :class:`PeriodReturn` value-object shared by every section that
emits a dated-return series (monthly / weekly / day-of-week / hour).

All fields default to empty / zero so an analyzer fed degenerate input
(flat curve, no trades, no benchmark) still produces a fully-formed,
JSON-serialisable model.

The actual computation lives in :mod:`engine.core.analytics.analyzer`;
this module is data-only on purpose so the schema can be reused by the
API layer, DB layer, and tests without pulling in the math.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PeriodReturn(BaseModel):
    """One row of a dated-return series (month, week, weekday, hour)."""

    period: str
    return_pct: float = 0.0


class ReturnMetrics(BaseModel):
    """Return KPIs — taxonomy items 1-14."""

    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    annualized_return_pct: float = 0.0
    monthly_returns: list[PeriodReturn] = Field(default_factory=list)
    weekly_returns: list[PeriodReturn] = Field(default_factory=list)
    daily_returns: list[float] = Field(default_factory=list)
    best_day_pct: float = 0.0
    worst_day_pct: float = 0.0
    best_month_pct: float = 0.0
    worst_month_pct: float = 0.0
    positive_days_pct: float = 0.0
    positive_months_pct: float = 0.0
    alpha_pct: float | None = None
    benchmark_relative_return: float | None = None


__all__ = ["PeriodReturn", "ReturnMetrics"]
