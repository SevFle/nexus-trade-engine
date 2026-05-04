from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.plugins.sdk import BaseStrategy


class _ConcreteStrategy(BaseStrategy):
    name = "test_strategy"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[dict]:
        return [{"action": "buy", "symbol": "AAPL", "qty": 10}]


class _EmptyStrategy(BaseStrategy):
    name = "empty_strategy"

    def on_bar(self, state: Any, portfolio: Any) -> list[dict]:
        return []


class TestBaseStrategy:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BaseStrategy()

    def test_concrete_strategy_instantiation(self):
        strategy = _ConcreteStrategy()
        assert strategy.name == "test_strategy"
        assert strategy.version == "1.0.0"

    def test_on_bar_returns_orders(self):
        strategy = _ConcreteStrategy()
        mock_state = MagicMock()
        mock_portfolio = MagicMock()
        orders = strategy.on_bar(mock_state, mock_portfolio)
        assert len(orders) == 1
        assert orders[0]["action"] == "buy"

    def test_on_start_default_noop(self):
        strategy = _ConcreteStrategy()
        mock_portfolio = MagicMock()
        result = strategy.on_start(mock_portfolio)
        assert result is None

    def test_on_end_default_noop(self):
        strategy = _ConcreteStrategy()
        mock_portfolio = MagicMock()
        result = strategy.on_end(mock_portfolio)
        assert result is None

    def test_empty_strategy_returns_empty_list(self):
        strategy = _EmptyStrategy()
        result = strategy.on_bar(MagicMock(), MagicMock())
        assert result == []

    def test_default_name_and_version(self):
        class MinimalStrategy(BaseStrategy):
            def on_bar(self, state, portfolio):
                return []

        s = MinimalStrategy()
        assert s.name == "unnamed"
        assert s.version == "0.1.0"


class TestNexusTradeEngineImport:
    def test_import_nexus_trade_engine(self):
        import nexus_trade_engine

        assert nexus_trade_engine is not None

    def test_import_engine_via_nexus_trade_engine(self):
        import engine
        import nexus_trade_engine

        assert engine.__path__ is not None
