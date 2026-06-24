"""Top-level ``PerformanceReport`` — the 86-KPI envelope (gh#97).

Aggregates the eight section models into a single JSON-serialisable
container that the API layer returns and the DB layer persists. The
report is intentionally flat-of-sections (not flat-of-field) so each
category can be loaded / stored / cached independently.

``metric_count`` returns the number of distinct KPI fields carried by
the report so consumers can assert the full 86 are present.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from engine.core.analytics.costs import CostMetrics
from engine.core.analytics.drawdown import DrawdownMetrics
from engine.core.analytics.positions import PositionMetrics
from engine.core.analytics.returns import ReturnMetrics
from engine.core.analytics.risk_adjusted import RiskAdjustedMetrics
from engine.core.analytics.time_analysis import TimeAnalysis
from engine.core.analytics.trades import TradeMetrics
from engine.core.analytics.volatility import VolatilityMetrics


class PerformanceReport(BaseModel):
    """Full 86-KPI performance report (eight sections).

    Every section defaults to its empty form, so
    ``PerformanceReport()`` is a valid, all-zero report — useful for
    error / placeholder responses in the API layer.
    """

    returns: ReturnMetrics = Field(default_factory=ReturnMetrics)
    """Return metrics — taxonomy items 1-14."""

    risk_adjusted: RiskAdjustedMetrics = Field(default_factory=RiskAdjustedMetrics)
    """Risk-adjusted ratio metrics — taxonomy items 15-24."""

    drawdown: DrawdownMetrics = Field(default_factory=DrawdownMetrics)
    """Drawdown metrics — taxonomy items 25-34."""

    trades: TradeMetrics = Field(default_factory=TradeMetrics)
    """Trade-level metrics — taxonomy items 35-50."""

    costs: CostMetrics = Field(default_factory=CostMetrics)
    """Cost & execution metrics — taxonomy items 51-58."""

    positions: PositionMetrics = Field(default_factory=PositionMetrics)
    """Position & exposure metrics — taxonomy items 59-66."""

    volatility: VolatilityMetrics = Field(default_factory=VolatilityMetrics)
    """Volatility & distribution metrics — taxonomy items 67-76."""

    time_analysis: TimeAnalysis = Field(default_factory=TimeAnalysis)
    """Time-based chart datasets — taxonomy items 77-86."""

    @property
    def metric_count(self) -> int:
        """Count of distinct scalar KPI fields across all sections.

        Excludes the bulk chart datasets in :class:`TimeAnalysis`
        (those are data series, not scalar KPIs) so the count reflects
        the headline 86-KPI taxonomy.
        """
        sections = [
            self.returns,
            self.risk_adjusted,
            self.drawdown,
            self.trades,
            self.costs,
            self.positions,
            self.volatility,
        ]
        return sum(len(type(s).model_fields) for s in sections)


__all__ = [
    "CostMetrics",
    "DrawdownMetrics",
    "PerformanceReport",
    "PositionMetrics",
    "ReturnMetrics",
    "RiskAdjustedMetrics",
    "TimeAnalysis",
    "TradeMetrics",
    "VolatilityMetrics",
]
