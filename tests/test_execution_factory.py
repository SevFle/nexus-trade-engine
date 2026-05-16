"""Tests for the ExecutionBackendFactory."""

from __future__ import annotations

import pytest

from engine.core.execution.backtest import BacktestBackend
from engine.core.execution.factory import (
    BackendNotAvailableError,
    ConfigurationError,
    ExecutionBackendFactory,
    ExecutionMode,
    create_execution_backend,
)
from engine.core.execution.paper import PaperBackend


class TestExecutionMode:
    def test_backtest_value(self):
        assert ExecutionMode.BACKTEST == "backtest"

    def test_paper_trade_value(self):
        assert ExecutionMode.PAPER_TRADE == "paper_trade"

    def test_live_value(self):
        assert ExecutionMode.LIVE == "live"


class TestFactoryCreation:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_singleton(self):
        f1 = ExecutionBackendFactory.get_instance()
        f2 = ExecutionBackendFactory.get_instance()
        assert f1 is f2

    def test_reset_creates_new_instance(self):
        f1 = ExecutionBackendFactory.get_instance()
        ExecutionBackendFactory.reset_instance()
        f2 = ExecutionBackendFactory.get_instance()
        assert f1 is not f2


class TestBackendCreation:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_create_backtest(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest")
        assert isinstance(backend, BacktestBackend)

    def test_create_paper_trade(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade")
        assert isinstance(backend, PaperBackend)

    def test_create_live_raises(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(BackendNotAvailableError, match="not yet implemented"):
            factory.create_backend("live")

    def test_invalid_mode_raises(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="Invalid execution mode"):
            factory.create_backend("invalid")

    def test_backtest_with_config(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "backtest",
            {"fill_probability": 0.9, "random_seed": 42},
        )
        assert isinstance(backend, BacktestBackend)
        assert backend.fill_probability == 0.9

    def test_paper_with_config(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {"fill_probability": 0.8, "latency_ms": 100.0},
        )
        assert isinstance(backend, PaperBackend)

    def test_backtest_invalid_fill_probability_raises(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="fill_probability"):
            factory.create_backend("backtest", {"fill_probability": 2.0})

    def test_paper_negative_latency_raises(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="latency_ms"):
            factory.create_backend("paper_trade", {"latency_ms": -10.0})

    def test_paper_invalid_slippage_model_raises(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="slippage_model_type"):
            factory.create_backend("paper_trade", {"slippage_model_type": "bogus"})


class TestConvenienceFunction:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_create_execution_backend(self):
        backend = create_execution_backend("backtest")
        assert isinstance(backend, BacktestBackend)

    def test_create_execution_backend_with_config(self):
        backend = create_execution_backend(
            "paper_trade",
            {"fill_probability": 0.9},
        )
        assert isinstance(backend, PaperBackend)


class TestRegisterBackend:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_register_custom_backend(self):
        from engine.core.execution.base import ExecutionBackend, FillResult

        class CustomBackend(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, CustomBackend)
        backend = factory.create_backend("backtest")
        assert isinstance(backend, CustomBackend)
