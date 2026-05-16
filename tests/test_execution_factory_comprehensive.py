"""
Comprehensive tests for ExecutionBackendFactory — covers the registry-based
backend selection refactor (commit 9466c4c), config validation edge cases,
custom backend registration fallback path, and PaperTradeExecutionBackend
creation via factory.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.core.execution.backtest import BacktestBackend
from engine.core.execution.base import ExecutionBackend, FillResult
from engine.core.execution.factory import (
    BackendNotAvailableError,
    ConfigurationError,
    ExecutionBackendFactory,
    ExecutionMode,
    _validate_backtest_config,
    _validate_paper_config,
    create_execution_backend,
)
from engine.core.execution.paper import PaperBackend
from engine.core.execution.paper_trade_backend import PaperTradeExecutionBackend
from engine.core.execution.slippage import SlippageModelType


class _SimpleCustomBackend(ExecutionBackend):
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def execute(self, order: Any, market_price: float, costs: Any) -> FillResult:
        return FillResult(success=True)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


class _CustomBackendNoKwargs(ExecutionBackend):
    def __init__(self) -> None:
        self.called = True

    async def execute(self, order: Any, market_price: float, costs: Any) -> FillResult:
        return FillResult(success=True)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


class TestExecutionModeEnum:
    def test_all_modes_defined(self):
        assert ExecutionMode.BACKTEST == "backtest"
        assert ExecutionMode.PAPER_TRADE == "paper_trade"
        assert ExecutionMode.LIVE == "live"

    def test_from_string(self):
        assert ExecutionMode("backtest") is ExecutionMode.BACKTEST
        assert ExecutionMode("paper_trade") is ExecutionMode.PAPER_TRADE
        assert ExecutionMode("live") is ExecutionMode.LIVE

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            ExecutionMode("invalid_mode")

    def test_is_str_enum(self):
        assert isinstance(ExecutionMode.BACKTEST, str)

    def test_iteration(self):
        modes = list(ExecutionMode)
        assert len(modes) == 3


class TestConfigurationErrors:
    def test_configuration_error_is_exception(self):
        assert issubclass(ConfigurationError, Exception)

    def test_backend_not_available_is_configuration_error(self):
        assert issubclass(BackendNotAvailableError, ConfigurationError)

    def test_raising_configuration_error(self):
        with pytest.raises(ConfigurationError, match="test"):
            raise ConfigurationError("test message")

    def test_raising_backend_not_available(self):
        with pytest.raises(BackendNotAvailableError):
            raise BackendNotAvailableError("not available")


class TestValidateBacktestConfig:
    def test_empty_config_passes(self):
        _validate_backtest_config({})

    def test_valid_random_seed_int(self):
        _validate_backtest_config({"random_seed": 42})

    def test_valid_random_seed_none(self):
        _validate_backtest_config({"random_seed": None})

    def test_invalid_random_seed_string(self):
        with pytest.raises(ConfigurationError, match="random_seed"):
            _validate_backtest_config({"random_seed": "not_a_number"})

    def test_invalid_random_seed_float(self):
        with pytest.raises(ConfigurationError, match="random_seed"):
            _validate_backtest_config({"random_seed": 3.14})

    def test_invalid_random_seed_list(self):
        with pytest.raises(ConfigurationError, match="random_seed"):
            _validate_backtest_config({"random_seed": [1, 2, 3]})

    def test_valid_fill_probability_lower_boundary(self):
        _validate_backtest_config({"fill_probability": 0.0})

    def test_valid_fill_probability_upper_boundary(self):
        _validate_backtest_config({"fill_probability": 1.0})

    def test_valid_fill_probability_middle(self):
        _validate_backtest_config({"fill_probability": 0.5})

    def test_valid_fill_probability_int(self):
        _validate_backtest_config({"fill_probability": 1})

    def test_invalid_fill_probability_negative(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_backtest_config({"fill_probability": -0.1})

    def test_invalid_fill_probability_above_one(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_backtest_config({"fill_probability": 1.1})

    def test_invalid_fill_probability_string(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_backtest_config({"fill_probability": "high"})

    def test_invalid_fill_probability_none(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_backtest_config({"fill_probability": None})

    def test_unknown_keys_ignored(self):
        _validate_backtest_config({"unknown_key": "value"})

    def test_combined_valid_config(self):
        _validate_backtest_config({
            "random_seed": 42,
            "fill_probability": 0.95,
            "partial_fill_enabled": True,
        })


class TestValidatePaperConfig:
    def test_empty_config_passes(self):
        _validate_paper_config({})

    def test_valid_fill_probability(self):
        _validate_paper_config({"fill_probability": 0.95})

    def test_invalid_fill_probability_negative(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_paper_config({"fill_probability": -0.1})

    def test_invalid_fill_probability_above_one(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_paper_config({"fill_probability": 1.5})

    def test_invalid_fill_probability_string(self):
        with pytest.raises(ConfigurationError, match="fill_probability"):
            _validate_paper_config({"fill_probability": "auto"})

    def test_valid_latency_ms_zero(self):
        _validate_paper_config({"latency_ms": 0})

    def test_valid_latency_ms_positive(self):
        _validate_paper_config({"latency_ms": 50.0})

    def test_invalid_latency_ms_negative(self):
        with pytest.raises(ConfigurationError, match="latency_ms"):
            _validate_paper_config({"latency_ms": -1.0})

    def test_invalid_latency_ms_string(self):
        with pytest.raises(ConfigurationError, match="latency_ms"):
            _validate_paper_config({"latency_ms": "fast"})

    def test_valid_slippage_model_type(self):
        _validate_paper_config({"slippage_model_type": "fixed_bps"})
        _validate_paper_config({"slippage_model_type": "percentage"})
        _validate_paper_config({"slippage_model_type": "square_root"})
        _validate_paper_config({"slippage_model_type": "volume_weighted"})
        _validate_paper_config({"slippage_model_type": "random_walk"})

    def test_invalid_slippage_model_type(self):
        with pytest.raises(ConfigurationError, match="slippage_model_type"):
            _validate_paper_config({"slippage_model_type": "bogus_model"})

    def test_all_invalid_simultaneously(self):
        with pytest.raises(ConfigurationError):
            _validate_paper_config({
                "fill_probability": 5.0,
                "latency_ms": -10,
                "slippage_model_type": "invalid",
            })


class TestFactorySingleton:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_get_instance_returns_factory(self):
        factory = ExecutionBackendFactory.get_instance()
        assert isinstance(factory, ExecutionBackendFactory)

    def test_singleton_identity(self):
        f1 = ExecutionBackendFactory.get_instance()
        f2 = ExecutionBackendFactory.get_instance()
        assert f1 is f2

    def test_reset_clears_singleton(self):
        f1 = ExecutionBackendFactory.get_instance()
        ExecutionBackendFactory.reset_instance()
        f2 = ExecutionBackendFactory.get_instance()
        assert f1 is not f2

    def test_reset_idempotent(self):
        ExecutionBackendFactory.reset_instance()
        ExecutionBackendFactory.reset_instance()
        factory = ExecutionBackendFactory.get_instance()
        assert isinstance(factory, ExecutionBackendFactory)

    def test_direct_instantiation_always_new(self):
        f1 = ExecutionBackendFactory()
        f2 = ExecutionBackendFactory()
        assert f1 is not f2


class TestFactoryRegistry:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_default_registry_has_backtest(self):
        factory = ExecutionBackendFactory()
        assert ExecutionMode.BACKTEST in factory._registry

    def test_default_registry_has_paper(self):
        factory = ExecutionBackendFactory()
        assert ExecutionMode.PAPER_TRADE in factory._registry

    def test_default_registry_no_live(self):
        factory = ExecutionBackendFactory()
        assert ExecutionMode.LIVE not in factory._registry

    def test_register_custom_backend(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        assert factory._registry[ExecutionMode.BACKTEST] is _SimpleCustomBackend

    def test_register_new_mode(self):
        factory = ExecutionBackendFactory()
        custom_mode = ExecutionMode.BACKTEST
        factory.register_backend(custom_mode, _SimpleCustomBackend)
        assert factory._registry[custom_mode] is _SimpleCustomBackend


class TestFactoryCreateBackendBacktest:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_creates_backtest_from_string(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest")
        assert isinstance(backend, BacktestBackend)

    def test_creates_backtest_from_enum(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(ExecutionMode.BACKTEST)
        assert isinstance(backend, BacktestBackend)

    def test_default_fill_probability(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest")
        assert backend.fill_probability == 0.98

    def test_custom_fill_probability(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"fill_probability": 0.85})
        assert backend.fill_probability == 0.85

    def test_default_partial_fill_enabled(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest")
        assert backend.partial_fill_enabled is True

    def test_custom_partial_fill(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"partial_fill_enabled": False})
        assert backend.partial_fill_enabled is False

    def test_custom_random_seed(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"random_seed": 42})
        assert isinstance(backend, BacktestBackend)

    def test_config_none_uses_defaults(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", None)
        assert isinstance(backend, BacktestBackend)
        assert backend.fill_probability == 0.98

    def test_empty_config_uses_defaults(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {})
        assert isinstance(backend, BacktestBackend)
        assert backend.fill_probability == 0.98


class TestFactoryCreateBackendPaper:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_creates_paper_from_string(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade")
        assert isinstance(backend, PaperBackend)

    def test_creates_paper_from_enum(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(ExecutionMode.PAPER_TRADE)
        assert isinstance(backend, PaperBackend)

    def test_paper_with_fill_probability(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"fill_probability": 0.8})
        assert isinstance(backend, PaperBackend)
        assert backend.config.fill_probability == 0.8

    def test_paper_with_latency_ms(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"latency_ms": 100.0})
        assert isinstance(backend, PaperBackend)
        assert backend.config.latency_ms == 100.0

    def test_paper_with_random_seed(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"random_seed": 123})
        assert isinstance(backend, PaperBackend)

    def test_paper_with_string_slippage_model_type(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"slippage_model_type": "percentage"})
        assert isinstance(backend, PaperBackend)

    def test_paper_with_enum_slippage_model_type(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {"slippage_model_type": SlippageModelType.SQUARE_ROOT},
        )
        assert isinstance(backend, PaperBackend)

    def test_paper_with_data_provider_kwarg(self):
        factory = ExecutionBackendFactory()
        provider = MagicMock()
        backend = factory.create_backend("paper_trade", {}, data_provider=provider)
        assert isinstance(backend, PaperBackend)


class TestFactoryCreateBackendPaperTradeFull:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_creates_full_backend_via_config_flag(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {"use_full_backend": True},
        )
        assert isinstance(backend, PaperTradeExecutionBackend)

    def test_creates_full_backend_via_kwarg(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {},
            use_full_backend=True,
        )
        assert isinstance(backend, PaperTradeExecutionBackend)

    def test_full_backend_default_initial_cash(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {"use_full_backend": True},
        )
        assert isinstance(backend, PaperTradeExecutionBackend)
        snapshot = backend.position_tracker.get_snapshot()
        assert snapshot.cash == 100_000.0

    def test_full_backend_custom_initial_cash(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {"use_full_backend": True, "initial_cash": 50_000.0},
        )
        assert isinstance(backend, PaperTradeExecutionBackend)
        snapshot = backend.position_tracker.get_snapshot()
        assert snapshot.cash == 50_000.0

    def test_full_backend_with_all_kwargs(self):
        factory = ExecutionBackendFactory()
        mock_provider = MagicMock()
        mock_clock = MagicMock()
        backend = factory.create_backend(
            "paper_trade",
            {"use_full_backend": True},
            data_provider=mock_provider,
            clock=mock_clock,
        )
        assert isinstance(backend, PaperTradeExecutionBackend)
        assert backend.clock is mock_clock

    def test_full_backend_with_risk_config(self):
        factory = ExecutionBackendFactory()

        risk_config = {
            "max_position_size": 500,
            "max_orders_per_minute": 30,
            "max_daily_loss_pct": 0.03,
        }
        backend = factory.create_backend(
            "paper_trade",
            {
                "use_full_backend": True,
                "risk_config": risk_config,
            },
        )
        assert isinstance(backend, PaperTradeExecutionBackend)

    def test_full_backend_with_slippage_config(self):
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade",
            {
                "use_full_backend": True,
                "slippage_model_type": "square_root",
                "slippage_model_kwargs": {"base_bps": 10.0, "volume_scale": 0.2},
            },
        )
        assert isinstance(backend, PaperTradeExecutionBackend)


class TestFactoryCreateBackendLive:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_live_mode_raises_not_available(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(BackendNotAvailableError, match="not yet implemented"):
            factory.create_backend("live")

    def test_live_mode_from_enum_raises(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(BackendNotAvailableError, match="not yet implemented"):
            factory.create_backend(ExecutionMode.LIVE)

    def test_live_error_mentions_alternatives(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(BackendNotAvailableError, match=r"backtest.*paper_trade"):
            factory.create_backend("live")


class TestFactoryCreateBackendInvalidMode:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_invalid_string_mode(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="Invalid execution mode"):
            factory.create_backend("nonexistent")

    def test_empty_string_mode(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="Invalid execution mode"):
            factory.create_backend("")

    def test_error_lists_valid_modes(self):
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="backtest") as exc_info:
            factory.create_backend("bogus")
        msg = str(exc_info.value)
        assert "paper_trade" in msg
        assert "live" in msg


class TestFactoryCustomBackendFallback:
    """Tests for the new registry-based fallback: return backend_cls(**config, **kwargs)."""

    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_custom_backend_uses_fallback_path(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        backend = factory.create_backend("backtest")
        assert isinstance(backend, _SimpleCustomBackend)

    def test_custom_backend_receives_config(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        backend = factory.create_backend("backtest", {"custom_key": "custom_value"})
        assert isinstance(backend, _SimpleCustomBackend)
        assert backend.kwargs.get("custom_key") == "custom_value"

    def test_custom_backend_receives_kwargs(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        backend = factory.create_backend(
            "backtest", {}, extra_param="extra_value",
        )
        assert isinstance(backend, _SimpleCustomBackend)
        assert backend.kwargs.get("extra_param") == "extra_value"

    def test_custom_backend_receives_both_config_and_kwargs(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        backend = factory.create_backend(
            "backtest",
            {"from_config": True},
            from_kwargs=True,
        )
        assert isinstance(backend, _SimpleCustomBackend)
        assert backend.kwargs.get("from_config") is True
        assert backend.kwargs.get("from_kwargs") is True

    def test_custom_backend_no_validation_skipped(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        backend = factory.create_backend("backtest", {"fill_probability": 5.0})
        assert isinstance(backend, _SimpleCustomBackend)
        assert backend.kwargs.get("fill_probability") == 5.0

    def test_register_overrides_default_backend(self):
        factory = ExecutionBackendFactory()
        assert factory._registry[ExecutionMode.BACKTEST] is BacktestBackend
        factory.register_backend(ExecutionMode.BACKTEST, _SimpleCustomBackend)
        assert factory._registry[ExecutionMode.BACKTEST] is _SimpleCustomBackend
        backend = factory.create_backend("backtest")
        assert isinstance(backend, _SimpleCustomBackend)

    def test_register_paper_override(self):
        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.PAPER_TRADE, _SimpleCustomBackend)
        backend = factory.create_backend("paper_trade")
        assert isinstance(backend, _SimpleCustomBackend)


class TestFactoryUnregisteredMode:
    """Test the 'No backend registered for mode' path (line 112)."""

    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_unregistered_mode_raises(self):
        factory = ExecutionBackendFactory()
        del factory._registry[ExecutionMode.BACKTEST]
        with pytest.raises(ConfigurationError, match="No backend registered"):
            factory.create_backend("backtest")


class TestConvenienceFunction:
    def setup_method(self):
        ExecutionBackendFactory.reset_instance()

    def test_creates_backtest(self):
        backend = create_execution_backend("backtest")
        assert isinstance(backend, BacktestBackend)

    def test_creates_paper(self):
        backend = create_execution_backend("paper_trade")
        assert isinstance(backend, PaperBackend)

    def test_with_config(self):
        backend = create_execution_backend("backtest", {"fill_probability": 0.9})
        assert isinstance(backend, BacktestBackend)
        assert backend.fill_probability == 0.9

    def test_with_kwargs(self):
        provider = MagicMock()
        backend = create_execution_backend(
            "paper_trade", {}, data_provider=provider,
        )
        assert isinstance(backend, PaperBackend)

    def test_invalid_mode_raises(self):
        with pytest.raises(ConfigurationError):
            create_execution_backend("bad_mode")

    def test_uses_singleton_factory(self):
        f1 = create_execution_backend("backtest")
        f2 = create_execution_backend("backtest")
        assert isinstance(f1, BacktestBackend)
        assert isinstance(f2, BacktestBackend)
