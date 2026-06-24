"""Drawdown section of the performance report (gh#97).

Pydantic model for the 10 drawdown KPIs (taxonomy items 25-34). The
``drawdown_curve`` is the per-bar underwater series (negative fractions
for charting) — emitted here so the report carries a single source of
truth that the API layer can stream to the underwater chart.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DrawdownMetrics(BaseModel):
    """Drawdown KPIs — taxonomy items 25-34."""

    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: int = 0
    avg_drawdown_pct: float = 0.0
    avg_drawdown_duration_days: float = 0.0
    recovery_factor: float | None = None
    ulcer_index: float = 0.0
    ulcer_performance_index: float | None = None
    pain_index: float = 0.0
    pain_ratio: float | None = None
    drawdown_curve: list[float] = Field(default_factory=list)


__all__ = ["DrawdownMetrics"]
