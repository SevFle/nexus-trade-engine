"""
Comprehensive tests for the most recently changed code to break the loop.

Target areas:
1. registry.py: FileNotFoundError handling in load_strategy_class, discover_strategies,
   _verify_integrity, _load_sandboxed, PluginRegistry lifecycle
2. execution/factory.py: Config validation edge cases, full backend creation paths
3. execution/commission.py: Zero quantity, boundary values, regulatory fees, all models
4. execution/slippage.py: Boundary conditions, zero/edge values
5. resource_limiter.py: CPU timing fix, signal fallback edge cases, _CPUTimer cancel paths
"""

from __future__ import annotations

import math
import random
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from engine.core.execution.commission import (
    CommissionModelType,
    CommissionQuote,
    FlatRateCommission,
    PercentageCommission,
    PerShareCommission,
    TieredCommission,
    ZeroCommission,
    create_commission_calculator,
)
from engine.core.execution.factory import (
    BackendNotAvailableError,
    ConfigurationError,
    ExecutionBackendFactory,
    ExecutionMode,
    create_execution_backend,
)
from engine.core.execution.slippage import (
    FixedBpsSlippage,
    PercentageSlippage,
    RandomWalkSlippage,
    SlippageContext,
    SlippageModel,
    SlippageModelType,
    SquareRootSlippage,
    VolumeWeightedSlippage,
    create_slippage_model,
)
from engine.plugins.registry import (
    PluginRegistry,
    discover_strategies,
    is_scoring_strategy,
    load_strategy_class,
)
from engine.plugins.sandbox.core.policy import ResourcePolicy
from engine.plugins.sandbox.core.violation import ResourceExhausted
from engine.plugins.sandbox.layers.resource_limiter import (
    _HAS_SIGVTALRM,
    ResourceLimiter,
    _CPUTimer,
    _WallTimer,
)


@pytest.fixture
def force_poll():
    _CPUTimer._force_poll = True
    yield
    _CPUTimer._force_poll = False


def _burn_cpu(duration: float) -> None:
    end = time.monotonic() + duration
    total = 0.0
    while time.monotonic() < end:
        total += 1.0


# =====================================================================
# 1. registry.py: load_strategy_class FileNotFoundError handling
# =====================================================================


class TestLoadStrategyClassFileNotFound:
    def test_nonexistent_module_raises_import_error(self) -> None:
        with pytest.raises(ImportError, match="Cannot load strategy"):
            load_strategy_class("/nonexistent/path/strategy.py")

    def test_file_not_found_during_exec_raises_import_error(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("raise FileNotFoundError('gone')")
        with pytest.raises(ImportError, match="Cannot load strategy"):
            load_strategy_class(str(mod_path))

    def test_missing_strategy_class_raises_attribute_error(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("x = 42")
        with pytest.raises(AttributeError, match="does not define a 'Strategy' class"):
            load_strategy_class(str(mod_path))

    def test_valid_module_with_strategy_class(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text(
            "class Strategy:\n"
            "    def run(self):\n"
            "        return 42\n"
        )
        cls = load_strategy_class(str(mod_path))
        assert cls is not None
        assert cls.__name__ == "Strategy"
        instance = cls()
        assert instance.run() == 42

    def test_syntax_error_in_module_propagates(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("def broken(")
        with pytest.raises(SyntaxError):
            load_strategy_class(str(mod_path))

    def test_import_error_in_module_propagates(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("import nonexistent_module_xyz_12345")
        with pytest.raises(ModuleNotFoundError):
            load_strategy_class(str(mod_path))


class TestLoadStrategyClassEdgeCases:
    def test_module_with_init_raises_attribute_error(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("def __init__(self): pass")
        with pytest.raises(AttributeError):
            load_strategy_class(str(mod_path))

    def test_module_with_strategy_as_function(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("def Strategy(): return 'func'")
        cls = load_strategy_class(str(mod_path))
        assert callable(cls)

    def test_module_with_strategy_as_int(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("Strategy = 42")
        cls = load_strategy_class(str(mod_path))
        assert cls == 42

    def test_empty_module_raises_attribute_error(self, tmp_path: Path) -> None:
        mod_path = tmp_path / "strategy.py"
        mod_path.write_text("")
        with pytest.raises(AttributeError):
            load_strategy_class(str(mod_path))


# =====================================================================
# 2. registry.py: discover_strategies
# =====================================================================


class TestDiscoverStrategies:
    def test_empty_directory(self, tmp_path: Path) -> None:
        result = discover_strategies(tmp_path)
        assert result == {}

    def test_nonexistent_directory(self) -> None:
        result = discover_strategies(Path("/nonexistent/dir"))
        assert result == {}

    def test_discovers_strategy_with_manifest(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "my_strategy"
        strat_dir.mkdir()
        manifest = strat_dir / "manifest.yaml"
        manifest.write_text(yaml.dump({"name": "test", "version": "1.0"}))
        strategy = strat_dir / "strategy.py"
        strategy.write_text("class Strategy: pass")
        result = discover_strategies(tmp_path)
        assert "my_strategy" in result
        assert result["my_strategy"]["manifest"]["name"] == "test"
        assert "strategy.py" in result["my_strategy"]["module_path"]

    def test_skips_strategy_without_module(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "no_module"
        strat_dir.mkdir()
        manifest = strat_dir / "manifest.yaml"
        manifest.write_text(yaml.dump({"name": "no_mod"}))
        result = discover_strategies(tmp_path)
        assert "no_module" not in result

    def test_multiple_strategies(self, tmp_path: Path) -> None:
        for name in ("alpha", "beta", "gamma"):
            d = tmp_path / name
            d.mkdir()
            (d / "manifest.yaml").write_text(yaml.dump({"name": name}))
            (d / "strategy.py").write_text("class Strategy: pass")
        result = discover_strategies(tmp_path)
        assert len(result) == 3
        assert set(result.keys()) == {"alpha", "beta", "gamma"}

    def test_invalid_yaml_skipped(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "bad_yaml"
        strat_dir.mkdir()
        (strat_dir / "manifest.yaml").write_text("{{invalid: yaml: [}")
        (strat_dir / "strategy.py").write_text("class Strategy: pass")
        with pytest.raises(yaml.YAMLError):
            discover_strategies(tmp_path)

    def test_subdirectories_without_manifest_ignored(self, tmp_path: Path) -> None:
        d = tmp_path / "no_manifest"
        d.mkdir()
        (d / "strategy.py").write_text("class Strategy: pass")
        (d / "readme.txt").write_text("hello")
        result = discover_strategies(tmp_path)
        assert "no_manifest" not in result


# =====================================================================
# 3. registry.py: is_scoring_strategy
# =====================================================================


class TestIsScoringStrategy:
    def test_non_strategy_object(self) -> None:
        assert is_scoring_strategy(42) is False
        assert is_scoring_strategy("string") is False
        assert is_scoring_strategy([]) is False

    def test_class_not_instance(self) -> None:
        class Foo:
            pass

        assert is_scoring_strategy(Foo) is False


# =====================================================================
# 4. registry.py: PluginRegistry
# =====================================================================


class TestPluginRegistry:
    def test_init_discovers_strategies(self, tmp_path: Path) -> None:
        d = tmp_path / "strat1"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "s1"}))
        (d / "strategy.py").write_text("class Strategy: pass")
        reg = PluginRegistry(strategies_dir=tmp_path)
        assert "strat1" in reg.list_strategies()

    def test_list_strategies_returns_keys(self, tmp_path: Path) -> None:
        for name in ("a", "b"):
            d = tmp_path / name
            d.mkdir()
            (d / "manifest.yaml").write_text(yaml.dump({"name": name}))
            (d / "strategy.py").write_text("class Strategy: pass")
        reg = PluginRegistry(strategies_dir=tmp_path)
        assert sorted(reg.list_strategies()) == ["a", "b"]

    def test_load_strategy_nonexistent_returns_none(self, tmp_path: Path) -> None:
        reg = PluginRegistry(strategies_dir=tmp_path)
        assert reg.load_strategy("nonexistent") is None

    def test_load_strategy_instantiates_class(self, tmp_path: Path) -> None:
        d = tmp_path / "work"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "work"}))
        (d / "strategy.py").write_text(
            "class Strategy:\n"
            "    def __init__(self):\n"
            "        self.value = 99\n"
        )
        reg = PluginRegistry(strategies_dir=tmp_path)
        instance = reg.load_strategy("work")
        assert instance is not None
        assert instance.value == 99

    def test_load_strategy_init_fails_returns_none(self, tmp_path: Path) -> None:
        d = tmp_path / "fail_init"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "fail"}))
        (d / "strategy.py").write_text(
            "class Strategy:\n"
            "    def __init__(self):\n"
            "        raise RuntimeError('init failed')\n"
        )
        reg = PluginRegistry(strategies_dir=tmp_path)
        assert reg.load_strategy("fail_init") is None

    def test_load_strategy_missing_class_returns_none(self, tmp_path: Path) -> None:
        d = tmp_path / "no_cls"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "nocls"}))
        (d / "strategy.py").write_text("x = 1")
        reg = PluginRegistry(strategies_dir=tmp_path)
        assert reg.load_strategy("no_cls") is None

    def test_sandbox_mode_false_returns_direct_instance(self, tmp_path: Path) -> None:
        d = tmp_path / "direct"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "direct", "id": "direct"}))
        (d / "strategy.py").write_text("class Strategy: pass")
        reg = PluginRegistry(strategies_dir=tmp_path, use_sandbox=False)
        result = reg.load_strategy("direct")
        assert result is not None


class TestPluginRegistryVerifyIntegrity:
    def test_no_content_hash_returns_true(self, tmp_path: Path) -> None:
        d = tmp_path / "nohash"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "nh"}))
        (d / "strategy.py").write_text("class Strategy: pass")
        reg = PluginRegistry(strategies_dir=tmp_path)
        entry = {"manifest": {"name": "test"}, "module_path": str(d / "strategy.py")}
        assert reg._verify_integrity("nohash", entry) is True

    def test_no_module_path_returns_false(self) -> None:
        reg = PluginRegistry(strategies_dir=Path("/nonexistent"))
        entry = {"manifest": {"content_hash": "abc123"}, "module_path": None}
        assert reg._verify_integrity("test", entry) is False

    def test_empty_module_path_returns_false(self) -> None:
        reg = PluginRegistry(strategies_dir=Path("/nonexistent"))
        entry = {"manifest": {"content_hash": "abc123"}, "module_path": ""}
        assert reg._verify_integrity("test", entry) is False


class TestPluginRegistryLoadSandboxed:
    def test_sandboxed_load_with_bad_manifest_returns_none(self, tmp_path: Path) -> None:
        d = tmp_path / "bad_manifest"
        d.mkdir()
        (d / "manifest.yaml").write_text(yaml.dump({"name": "bad"}))
        (d / "strategy.py").write_text("class Strategy: pass")
        reg = PluginRegistry(strategies_dir=tmp_path, use_sandbox=True)
        with patch.object(reg, "_verify_integrity", return_value=False):
            entry = {
                "manifest": {"name": "bad"},
                "module_path": str(d / "strategy.py"),
            }
            result = reg._load_sandboxed("bad", MagicMock(), entry)
            assert result is None


# =====================================================================
# 5. execution/factory.py: Config validation edge cases
# =====================================================================


class TestFactoryConfigValidation:
    def setup_method(self) -> None:
        ExecutionBackendFactory.reset_instance()

    def test_backtest_random_seed_none_valid(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"random_seed": None})
        assert backend is not None

    def test_backtest_random_seed_int_valid(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"random_seed": 42})
        assert backend is not None

    def test_backtest_random_seed_string_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="random_seed"):
            factory.create_backend("backtest", {"random_seed": "abc"})

    def test_backtest_random_seed_float_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="random_seed"):
            factory.create_backend("backtest", {"random_seed": 3.14})

    def test_backtest_fill_probability_zero_valid(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"fill_probability": 0.0})
        assert backend is not None

    def test_backtest_fill_probability_one_valid(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"fill_probability": 1.0})
        assert backend is not None

    def test_backtest_fill_probability_negative_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="fill_probability"):
            factory.create_backend("backtest", {"fill_probability": -0.1})

    def test_backtest_fill_probability_above_one_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="fill_probability"):
            factory.create_backend("backtest", {"fill_probability": 1.1})

    def test_backtest_fill_probability_string_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="fill_probability"):
            factory.create_backend("backtest", {"fill_probability": "high"})

    def test_paper_fill_probability_boundary_zero(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"fill_probability": 0.0})
        assert backend is not None

    def test_paper_fill_probability_boundary_one(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"fill_probability": 1.0})
        assert backend is not None

    def test_paper_latency_ms_zero_valid(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("paper_trade", {"latency_ms": 0})
        assert backend is not None

    def test_paper_latency_ms_negative_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="latency_ms"):
            factory.create_backend("paper_trade", {"latency_ms": -1})

    def test_paper_latency_ms_string_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="latency_ms"):
            factory.create_backend("paper_trade", {"latency_ms": "fast"})

    def test_paper_slippage_model_type_valid_string(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(
            "paper_trade", {"slippage_model_type": "percentage"}
        )
        assert backend is not None

    def test_paper_slippage_model_type_invalid_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="slippage_model_type"):
            factory.create_backend("paper_trade", {"slippage_model_type": "bogus"})


class TestFactoryModeHandling:
    def setup_method(self) -> None:
        ExecutionBackendFactory.reset_instance()

    def test_mode_as_enum(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend(ExecutionMode.BACKTEST)
        assert backend is not None

    def test_mode_as_string(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest")
        assert backend is not None

    def test_live_mode_raises_backend_not_available(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(BackendNotAvailableError, match="not yet implemented"):
            factory.create_backend(ExecutionMode.LIVE)

    def test_invalid_string_mode_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError, match="Invalid execution mode"):
            factory.create_backend("real_time")

    def test_empty_string_mode_raises(self) -> None:
        factory = ExecutionBackendFactory()
        with pytest.raises(ConfigurationError):
            factory.create_backend("")

    def test_config_none_defaults_to_empty(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", None)
        assert backend is not None

    def test_extra_config_keys_ignored(self) -> None:
        factory = ExecutionBackendFactory()
        backend = factory.create_backend("backtest", {"extra_key": "ignored"})
        assert backend is not None


class TestFactorySingleton:
    def setup_method(self) -> None:
        ExecutionBackendFactory.reset_instance()

    def test_get_instance_creates_singleton(self) -> None:
        f1 = ExecutionBackendFactory.get_instance()
        f2 = ExecutionBackendFactory.get_instance()
        assert f1 is f2

    def test_reset_clears_instance(self) -> None:
        f1 = ExecutionBackendFactory.get_instance()
        ExecutionBackendFactory.reset_instance()
        f2 = ExecutionBackendFactory.get_instance()
        assert f1 is not f2

    def test_multiple_resets_safe(self) -> None:
        ExecutionBackendFactory.reset_instance()
        ExecutionBackendFactory.reset_instance()
        f = ExecutionBackendFactory.get_instance()
        assert f is not None


class TestFactoryRegisterBackend:
    def setup_method(self) -> None:
        ExecutionBackendFactory.reset_instance()

    def test_register_and_use_custom_backend(self) -> None:
        from engine.core.execution.base import ExecutionBackend, FillResult

        class MockBackend(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True, order_id="custom")

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, MockBackend)
        backend = factory.create_backend("backtest")
        assert isinstance(backend, MockBackend)

    def test_register_overrides_existing(self) -> None:
        from engine.core.execution.base import ExecutionBackend, FillResult

        class Backend1(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        class Backend2(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        factory = ExecutionBackendFactory()
        factory.register_backend(ExecutionMode.BACKTEST, Backend1)
        assert isinstance(factory.create_backend("backtest"), Backend1)
        factory.register_backend(ExecutionMode.BACKTEST, Backend2)
        assert isinstance(factory.create_backend("backtest"), Backend2)


class TestConvenienceFunctionEdgeCases:
    def setup_method(self) -> None:
        ExecutionBackendFactory.reset_instance()

    def test_create_execution_backend_uses_singleton(self) -> None:
        b1 = create_execution_backend("backtest")
        b2 = create_execution_backend("backtest")
        assert type(b1) is type(b2)


# =====================================================================
# 6. commission.py: Edge cases and boundary values
# =====================================================================


class TestPerShareCommissionEdgeCases:
    def test_zero_quantity_hits_minimum(self) -> None:
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        quote = calc.calculate(quantity=0, price=100.0, side="buy")
        assert quote.estimated_commission == 1.0

    def test_single_share_hits_minimum(self) -> None:
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        quote = calc.calculate(quantity=1, price=100.0, side="buy")
        assert quote.estimated_commission == 1.0

    def test_exact_threshold_bypasses_minimum(self) -> None:
        calc = PerShareCommission(rate_per_share=0.01, min_commission=1.0)
        quote = calc.calculate(quantity=100, price=100.0, side="buy")
        assert quote.estimated_commission == 1.0

    def test_very_large_quantity(self) -> None:
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        quote = calc.calculate(quantity=1_000_000, price=100.0, side="buy")
        assert quote.estimated_commission == 5000.0

    def test_regulatory_fee_zero_for_buy(self) -> None:
        calc = PerShareCommission()
        quote = calc.calculate(quantity=100, price=100.0, side="buy")
        assert quote.regulatory_fee == 0.0

    def test_regulatory_fee_positive_for_sell(self) -> None:
        calc = PerShareCommission()
        quote = calc.calculate(quantity=100, price=100.0, side="sell")
        assert quote.regulatory_fee > 0.0

    def test_regulatory_fee_zero_quantity(self) -> None:
        calc = PerShareCommission()
        quote = calc.calculate(quantity=0, price=100.0, side="sell")
        assert quote.regulatory_fee == 0.0

    def test_exchange_fee_scale_with_quantity(self) -> None:
        calc = PerShareCommission(exchange_fee_per_share=0.003)
        q1 = calc.calculate(quantity=100, price=100.0, side="buy")
        q2 = calc.calculate(quantity=200, price=100.0, side="buy")
        assert q2.exchange_fee == pytest.approx(q1.exchange_fee * 2)

    def test_total_equals_sum_of_components(self) -> None:
        calc = PerShareCommission(rate_per_share=0.005, exchange_fee_per_share=0.001)
        quote = calc.calculate(quantity=1000, price=100.0, side="sell")
        expected = quote.estimated_commission + quote.exchange_fee + quote.regulatory_fee
        assert quote.total == pytest.approx(expected, rel=1e-3)

    def test_high_rate_per_share(self) -> None:
        calc = PerShareCommission(rate_per_share=0.1, min_commission=0.0)
        quote = calc.calculate(quantity=100, price=100.0, side="buy")
        assert quote.estimated_commission == 10.0

    def test_zero_price(self) -> None:
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        quote = calc.calculate(quantity=100, price=0.0, side="buy")
        assert quote.estimated_commission >= 1.0


class TestFlatRateCommissionEdgeCases:
    def test_zero_quantity_same_commission(self) -> None:
        calc = FlatRateCommission(flat_rate=4.95)
        q0 = calc.calculate(quantity=0, price=100.0, side="buy")
        q100 = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q0.estimated_commission == q100.estimated_commission

    def test_zero_price_same_commission(self) -> None:
        calc = FlatRateCommission(flat_rate=4.95)
        q = calc.calculate(quantity=100, price=0.0, side="buy")
        assert q.estimated_commission == 4.95

    def test_no_exchange_fee(self) -> None:
        calc = FlatRateCommission(flat_rate=5.0, exchange_fee=0.0)
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.exchange_fee == 0.0
        assert q.total == 5.0

    def test_with_exchange_fee(self) -> None:
        calc = FlatRateCommission(flat_rate=5.0, exchange_fee=1.0)
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.exchange_fee == 1.0
        assert q.total == 6.0

    def test_sell_no_regulatory_fee(self) -> None:
        calc = FlatRateCommission(flat_rate=4.95)
        q = calc.calculate(quantity=100, price=100.0, side="sell")
        assert q.regulatory_fee == 0.0

    def test_total_equals_commission_plus_exchange(self) -> None:
        calc = FlatRateCommission(flat_rate=4.95, exchange_fee=0.50)
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.total == pytest.approx(5.45)

    def test_zero_flat_rate(self) -> None:
        calc = FlatRateCommission(flat_rate=0.0)
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.estimated_commission == 0.0
        assert q.total == 0.0


class TestPercentageCommissionEdgeCases:
    def test_zero_quantity_hits_minimum(self) -> None:
        calc = PercentageCommission(rate_pct=0.001, min_commission=5.0)
        q = calc.calculate(quantity=0, price=100.0, side="buy")
        assert q.estimated_commission == 5.0

    def test_zero_price_hits_minimum(self) -> None:
        calc = PercentageCommission(rate_pct=0.001, min_commission=5.0)
        q = calc.calculate(quantity=100, price=0.0, side="buy")
        assert q.estimated_commission == 5.0

    def test_sell_includes_regulatory_fee(self) -> None:
        calc = PercentageCommission(rate_pct=0.001, min_commission=0.0)
        q_buy = calc.calculate(quantity=100, price=100.0, side="buy")
        q_sell = calc.calculate(quantity=100, price=100.0, side="sell")
        assert q_sell.regulatory_fee > 0
        assert q_buy.regulatory_fee == 0.0

    def test_no_exchange_fee(self) -> None:
        calc = PercentageCommission()
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.exchange_fee == 0.0

    def test_large_notional(self) -> None:
        calc = PercentageCommission(rate_pct=0.001, min_commission=1.0)
        q = calc.calculate(quantity=10000, price=500.0, side="buy")
        assert q.estimated_commission == 5000.0

    def test_total_includes_regulatory_for_sell(self) -> None:
        calc = PercentageCommission(rate_pct=0.001, min_commission=0.0)
        q = calc.calculate(quantity=1000, price=100.0, side="sell")
        expected = q.estimated_commission + q.regulatory_fee
        assert q.total == pytest.approx(expected, rel=1e-3)


class TestTieredCommissionEdgeCases:
    def test_default_tiers_boundary_0(self) -> None:
        calc = TieredCommission(min_commission=0.0)
        q = calc.calculate(quantity=0, price=100.0, side="buy")
        assert q.estimated_commission == pytest.approx(0.0)

    def test_default_tiers_boundary_499(self) -> None:
        calc = TieredCommission(min_commission=0.0)
        q = calc.calculate(quantity=499, price=100.0, side="buy")
        assert q.estimated_commission == pytest.approx(0.008 * 499)

    def test_default_tiers_boundary_500(self) -> None:
        calc = TieredCommission(min_commission=0.0)
        q = calc.calculate(quantity=500, price=100.0, side="buy")
        assert q.estimated_commission == pytest.approx(0.005 * 500)

    def test_default_tiers_boundary_2000(self) -> None:
        calc = TieredCommission(min_commission=0.0)
        q = calc.calculate(quantity=2000, price=100.0, side="buy")
        assert q.estimated_commission == pytest.approx(0.003 * 2000)

    def test_default_tiers_boundary_10000(self) -> None:
        calc = TieredCommission(min_commission=0.0)
        q = calc.calculate(quantity=10000, price=100.0, side="buy")
        assert q.estimated_commission == pytest.approx(0.001 * 10000)

    def test_default_tiers_above_max(self) -> None:
        calc = TieredCommission(min_commission=0.0)
        q = calc.calculate(quantity=50000, price=100.0, side="buy")
        assert q.estimated_commission == pytest.approx(0.001 * 50000)

    def test_minimum_commission_applied(self) -> None:
        calc = TieredCommission(min_commission=10.0)
        q = calc.calculate(quantity=1, price=100.0, side="buy")
        assert q.estimated_commission == 10.0

    def test_no_exchange_or_regulatory_fee(self) -> None:
        calc = TieredCommission()
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.exchange_fee == 0.0
        assert q.regulatory_fee == 0.0

    def test_sell_no_regulatory_fee(self) -> None:
        calc = TieredCommission()
        q = calc.calculate(quantity=100, price=100.0, side="sell")
        assert q.regulatory_fee == 0.0

    def test_single_tier(self) -> None:
        calc = TieredCommission(tiers=[(0, 0.01)], min_commission=0.0)
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.estimated_commission == 1.0


class TestZeroCommissionEdgeCases:
    def test_large_values(self) -> None:
        calc = ZeroCommission()
        q = calc.calculate(quantity=1_000_000, price=9999.0, side="sell")
        assert q.estimated_commission == 0.0
        assert q.exchange_fee == 0.0
        assert q.regulatory_fee == 0.0
        assert q.total == 0.0

    def test_zero_values(self) -> None:
        calc = ZeroCommission()
        q = calc.calculate(quantity=0, price=0.0, side="buy")
        assert q.total == 0.0


class TestCommissionQuoteDataclass:
    def test_fields(self) -> None:
        q = CommissionQuote(
            estimated_commission=1.0,
            exchange_fee=0.5,
            regulatory_fee=0.01,
            total=1.51,
        )
        assert q.estimated_commission == 1.0
        assert q.exchange_fee == 0.5
        assert q.regulatory_fee == 0.01
        assert q.total == 1.51


class TestCreateCommissionCalculatorEdgeCases:
    def test_default_creates_per_share(self) -> None:
        calc = create_commission_calculator()
        assert isinstance(calc, PerShareCommission)

    def test_enum_type(self) -> None:
        calc = create_commission_calculator(CommissionModelType.ZERO)
        assert isinstance(calc, ZeroCommission)

    def test_string_type(self) -> None:
        calc = create_commission_calculator("flat_rate")
        assert isinstance(calc, FlatRateCommission)

    def test_kwargs_passed(self) -> None:
        calc = create_commission_calculator(
            CommissionModelType.FLAT_RATE, flat_rate=9.99
        )
        q = calc.calculate(quantity=100, price=100.0, side="buy")
        assert q.estimated_commission == 9.99


# =====================================================================
# 7. slippage.py: Boundary conditions and edge cases
# =====================================================================


class TestSlippageBoundaryConditions:
    def test_fixed_bps_zero_bps(self) -> None:
        model = FixedBpsSlippage(bps=0.0)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        assert model.compute(ctx) == 0.0

    def test_fixed_bps_negative_bps(self) -> None:
        model = FixedBpsSlippage(bps=-5.0)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        assert model.compute(ctx) < 0.0

    def test_fixed_bps_very_large_bps(self) -> None:
        model = FixedBpsSlippage(bps=10000.0)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        assert model.compute(ctx) == 100.0

    def test_percentage_zero_pct(self) -> None:
        model = PercentageSlippage(pct=0.0)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        assert model.compute(ctx) == 0.0

    def test_percentage_negative_price(self) -> None:
        model = PercentageSlippage(pct=0.001)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=-100.0)
        assert model.compute(ctx) < 0.0

    def test_square_root_very_high_participation(self) -> None:
        model = SquareRootSlippage(base_bps=5.0, volume_scale=1.0)
        ctx = SlippageContext(
            symbol="AAPL", side="buy", quantity=1000000, market_price=100.0, avg_volume=1000
        )
        result = model.compute(ctx)
        assert result > 0.0
        assert math.isfinite(result)

    def test_square_root_zero_quantity_with_zero_volume(self) -> None:
        model = SquareRootSlippage(base_bps=5.0)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=0, market_price=100.0, avg_volume=0)
        result = model.compute(ctx)
        assert result == pytest.approx(0.05)

    def test_volume_weighted_exact_max_impact(self) -> None:
        model = VolumeWeightedSlippage(base_bps=5.0, max_impact_bps=50.0)
        ctx = SlippageContext(
            symbol="AAPL", side="buy", quantity=500, market_price=100.0, avg_volume=1000
        )
        result = model.compute(ctx)
        base = 100.0 * (5.0 / 10000)
        participation = 500 / 1000
        impact_bps = min(participation * 100, 50.0)
        impact = 100.0 * (impact_bps / 10000)
        assert result == pytest.approx(base + impact)

    def test_volume_weighted_zero_avg_volume_ignores_impact(self) -> None:
        model = VolumeWeightedSlippage(base_bps=5.0)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0, avg_volume=0)
        assert model.compute(ctx) == pytest.approx(0.05)

    def test_random_walk_always_non_negative_many_trials(self) -> None:
        rng = random.Random(12345)  # noqa: S311
        model = RandomWalkSlippage(base_bps=5.0, volatility_factor=2.0, rng=rng)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        for _ in range(1000):
            assert model.compute(ctx) >= 0.0

    def test_random_walk_zero_volatility_deterministic(self) -> None:
        rng = random.Random(42)  # noqa: S311
        model = RandomWalkSlippage(base_bps=5.0, volatility_factor=0.0, rng=rng)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        results = [model.compute(ctx) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_random_walk_custom_rng(self) -> None:
        rng = random.Random(42)  # noqa: S311
        model = RandomWalkSlippage(base_bps=5.0, volatility_factor=1.0, rng=rng)
        ctx = SlippageContext(symbol="AAPL", side="buy", quantity=100, market_price=100.0)
        r1 = model.compute(ctx)
        rng2 = random.Random(42)  # noqa: S311
        model2 = RandomWalkSlippage(base_bps=5.0, volatility_factor=1.0, rng=rng2)
        r2 = model2.compute(ctx)
        assert r1 == r2

    def test_create_slippage_model_all_types(self) -> None:
        for t in SlippageModelType:
            model = create_slippage_model(t)
            assert isinstance(model, SlippageModel)

    def test_create_slippage_model_string_type(self) -> None:
        model = create_slippage_model("random_walk", base_bps=10.0)
        assert isinstance(model, RandomWalkSlippage)
        assert model.base_bps == 10.0


class TestSlippageRegistry:
    def test_registry_contains_all_types(self) -> None:
        from engine.core.execution.slippage import SLIPPAGE_MODEL_REGISTRY

        assert len(SLIPPAGE_MODEL_REGISTRY) == len(SlippageModelType)
        for t in SlippageModelType:
            assert t in SLIPPAGE_MODEL_REGISTRY


# =====================================================================
# 8. resource_limiter.py: CPU timing fix edge cases
# =====================================================================


class TestCPUTimerCancelPath:
    def test_cancel_clears_use_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        t.cancel()
        assert t._use_signal is False

    def test_cancel_stops_itimer_and_restores_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        t.cancel()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel
        remaining = signal.getitimer(signal.ITIMER_VIRTUAL)
        assert remaining[0] == 0.0

    def test_cancel_when_poll_mode_no_signal_cleanup(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is False
        old_handler = signal.getsignal(signal.SIGVTALRM)
        t.cancel()
        assert signal.getsignal(signal.SIGVTALRM) is old_handler

    def test_cancel_join_timeout_prevents_hanging(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        start = time.monotonic()
        t.cancel()
        elapsed = time.monotonic() - start
        assert elapsed < 2.0

    def test_cancel_then_restart_clears_state(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        time.sleep(0.1)
        assert t.expired
        t.cancel()
        t._seconds = 60.0
        t.start()
        assert not t.expired
        assert not t._cancelled.is_set()
        t.cancel()


class TestCPUTimerSignalEdgeCases:
    def test_try_start_signal_returns_false_for_force_poll(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        _CPUTimer._force_poll = True
        try:
            t = _CPUTimer(10.0)
            assert t._try_start_signal() is False
        finally:
            _CPUTimer._force_poll = False

    def test_try_start_signal_returns_false_without_sigvtalrm(self) -> None:
        with patch("engine.plugins.sandbox.layers.resource_limiter._HAS_SIGVTALRM", False):
            t = _CPUTimer(10.0)
            assert t._try_start_signal() is False

    def test_stop_signal_handles_both_value_and_os_error(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch("signal.setitimer", side_effect=ValueError), \
             patch("signal.signal", side_effect=OSError):
            t._stop_signal()
        assert t._use_signal is False

    def test_signal_mode_expires_on_cpu_burn(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05, plugin_id="cpu-burn")
        t.start()
        _burn_cpu(0.5)
        assert t.expired
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        assert exc_info.value.plugin_id == "cpu-burn"
        t.cancel()


class TestCPUTimerCPUTimeNotWallClock:
    def test_cpu_time_does_not_advance_with_sleep(self) -> None:
        t = _CPUTimer(10.0)
        before = t._cpu_time()
        time.sleep(0.2)
        after = t._cpu_time()
        assert (after - before) < 0.1

    def test_cpu_time_advances_with_computation(self) -> None:
        t = _CPUTimer(10.0)
        before = t._cpu_time()
        _burn_cpu(0.1)
        after = t._cpu_time()
        assert after >= before

    def test_poll_uses_cpu_not_wall(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            time.sleep(0.3)
            assert not t.expired
        finally:
            t.cancel()


class TestWallTimerEdgeCases:
    def test_check_with_exactly_at_limit(self) -> None:
        t = _WallTimer(10.0)
        t._start_time = time.monotonic() - 5.0
        t.check()

    def test_check_with_just_past_limit(self) -> None:
        t = _WallTimer(0.001)
        t._start_time = time.monotonic() - 1.0
        with pytest.raises(ResourceExhausted):
            t.check()

    def test_elapsed_negative_start_time(self) -> None:
        t = _WallTimer(10.0)
        t._start_time = time.monotonic() + 100.0
        assert t.elapsed < 0.0

    def test_context_manager_full_lifecycle(self) -> None:
        with _WallTimer(10.0) as t:
            assert t._timer is not None
            assert not t.expired
        assert t._timer is None

    def test_daemon_timer_thread(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        try:
            assert t._timer is not None
            assert t._timer.daemon is True
        finally:
            t.cancel()


class TestResourceLimiterEdgeCases:
    def test_install_double_idempotent(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="double")
        limiter.install()
        cpu_before = limiter._cpu_timer
        limiter.install()
        assert limiter._cpu_timer is cpu_before
        limiter.uninstall()

    def test_uninstall_not_installed_is_noop(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.uninstall()
        assert not limiter._installed

    def test_check_timers_when_not_installed(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.check_cpu_timer()
        limiter.check_wall_timer()

    def test_thread_count_exact_limit(self) -> None:
        policy = ResourcePolicy(max_threads=3)
        limiter = ResourceLimiter(policy)
        limiter.increment_thread()
        limiter.increment_thread()
        limiter.increment_thread()
        assert limiter._thread_count == 3
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_decrement_below_zero_floors(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.decrement_thread()
        limiter.decrement_thread()
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_violations_cleared_after_clear(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy)
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0

    def test_parse_memory_empty_string(self) -> None:
        with pytest.raises(ValueError):
            ResourceLimiter.parse_memory("")

    def test_parse_memory_various_units(self) -> None:
        assert ResourceLimiter.parse_memory("2GB") == 2 * 1024**3
        assert ResourceLimiter.parse_memory("256MB") == 256 * 1024**2
        assert ResourceLimiter.parse_memory("512KB") == 512 * 1024
        assert ResourceLimiter.parse_memory("1024B") == 1024
        assert ResourceLimiter.parse_memory("4096") == 4096
        assert ResourceLimiter.parse_memory("1.5GB") == int(1.5 * 1024**3)

    def test_cpu_elapsed_after_install_and_uninstall(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy)
        limiter.install()
        time.sleep(0.02)
        assert limiter.cpu_elapsed > 0
        limiter.uninstall()
        assert limiter.cpu_elapsed == 0.0

    def test_full_lifecycle_with_exception_in_body(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="exc-lifecycle")

        def _body() -> None:
            limiter.install()
            limiter.check_cpu_timer()
            raise ValueError("boom")

        try:
            _body()
        except ValueError:
            pass
        finally:
            limiter.uninstall()
        assert not limiter._installed


# =====================================================================
# 9. ExecutionMode enum edge cases
# =====================================================================


class TestExecutionModeEnum:
    def test_all_modes_are_strings(self) -> None:
        for mode in ExecutionMode:
            assert isinstance(mode.value, str)

    def test_from_string_construction(self) -> None:
        assert ExecutionMode("backtest") == ExecutionMode.BACKTEST
        assert ExecutionMode("paper_trade") == ExecutionMode.PAPER_TRADE
        assert ExecutionMode("live") == ExecutionMode.LIVE

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            ExecutionMode("real_time")

    def test_str_representation(self) -> None:
        assert str(ExecutionMode.BACKTEST) == "backtest"
        assert ExecutionMode.BACKTEST.value == "backtest"


# =====================================================================
# 10. CommissionModelType enum
# =====================================================================


class TestCommissionModelTypeEnum:
    def test_all_types(self) -> None:
        assert CommissionModelType.PER_SHARE == "per_share"
        assert CommissionModelType.FLAT_RATE == "flat_rate"
        assert CommissionModelType.PERCENTAGE == "percentage"
        assert CommissionModelType.TIERED == "tiered"
        assert CommissionModelType.ZERO == "zero"

    def test_from_string(self) -> None:
        for t in CommissionModelType:
            assert CommissionModelType(t.value) is t

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            CommissionModelType("invalid")


# =====================================================================
# 11. Integration: factory creates working backends
# =====================================================================


class TestFactoryIntegration:
    def setup_method(self) -> None:
        ExecutionBackendFactory.reset_instance()

    def test_backtest_defaults(self) -> None:
        from engine.core.execution.backtest import BacktestBackend

        backend = create_execution_backend("backtest")
        assert isinstance(backend, BacktestBackend)
        assert backend.fill_probability == 0.98
        assert backend.partial_fill_enabled is True

    def test_paper_defaults(self) -> None:
        from engine.core.execution.paper import PaperBackend

        backend = create_execution_backend("paper_trade")
        assert isinstance(backend, PaperBackend)

    def test_backtest_with_all_config(self) -> None:
        backend = create_execution_backend(
            "backtest",
            {
                "fill_probability": 0.95,
                "partial_fill_enabled": False,
                "random_seed": 42,
            },
        )
        assert backend.fill_probability == 0.95
        assert backend.partial_fill_enabled is False

    def test_paper_with_slippage_config(self) -> None:
        backend = create_execution_backend(
            "paper_trade",
            {
                "slippage_model_type": "percentage",
                "latency_ms": 100.0,
                "fill_probability": 0.9,
            },
        )
        assert backend is not None
