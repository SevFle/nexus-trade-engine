"""Paper-trading runner package.

Bridges live data feeds to :meth:`IStrategy.evaluate` (with cost-model
injection) and routes the resulting signals through the
:class:`~engine.core.order_manager.OrderManager` to a paper broker.

Public surface::

    from engine.paper_trade import PaperTradeRunner, PaperTradeConfig
"""

from __future__ import annotations

from engine.paper_trade.runner import (
    DataFeed,
    PaperTradeConfig,
    PaperTradeRunner,
    PaperTradeStats,
    StrategyLike,
)

__all__ = [
    "DataFeed",
    "PaperTradeConfig",
    "PaperTradeRunner",
    "PaperTradeStats",
    "StrategyLike",
]
