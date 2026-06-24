"""Cost & execution section of the performance report (gh#97).

Pydantic model for the 8 cost/execution KPIs (taxonomy items 51-58).
The analyzer rolls the per-trade :class:`~engine.core.cost_model.CostBreakdown`
entries up into ``cost_breakdown`` (component → dollars) for the pie
chart, and converts slippage / implementation shortfall into basis
points of traded notional.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CostMetrics(BaseModel):
    """Cost & execution KPIs — taxonomy items 51-58."""

    total_transaction_costs: float = 0.0
    total_taxes: float = 0.0
    cost_drag_pct: float = 0.0
    gross_vs_net_return_gap_pct: float = 0.0
    avg_cost_per_trade: float = 0.0
    avg_slippage_bps: float = 0.0
    avg_implementation_shortfall_bps: float = 0.0
    cost_breakdown: dict[str, float] = Field(default_factory=dict)


__all__ = ["CostMetrics"]
