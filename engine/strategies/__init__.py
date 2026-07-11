"""Strategy management layer.

Hosts higher-level coordinators that sit *above* individual
:class:`~sdk.nexus_sdk.strategy.IStrategy` plugins. Where
``engine.core.signal_aggregator`` is a pure voting helper and
``engine.core.strategy_orchestrator`` is a capital-free signal voter,
this package owns the concerns that bind strategies to *capital*:

per-strategy allocation budgets, provenance tracking, and capital-cap
enforcement over the signals a strategy emits.
"""

from engine.strategies.multi_manager import (
    MultiStrategyEvaluation,
    MultiStrategyManager,
    MultiStrategyManagerError,
    StrategyRegistration,
)

__all__ = [
    "MultiStrategyEvaluation",
    "MultiStrategyManager",
    "MultiStrategyManagerError",
    "StrategyRegistration",
]
