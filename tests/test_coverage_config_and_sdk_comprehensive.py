"""Comprehensive tests for the coverage configuration fix and recently changed SDK code.

Targets:
  - pyproject.toml [tool.coverage.run] source path regression guard
  - sdk/nexus_sdk/types.py (Money, CostBreakdown, PortfolioSnapshot)
  - sdk/nexus_sdk/scoring.py (ZScoreNormalizer, scoring models, IScoringStrategy)
  - sdk/nexus_sdk/signals.py (Signal factory methods, all kwargs)
  - sdk/nexus_sdk/strategy.py (MarketState indicators, IStrategy defaults)
  - sdk/nexus_sdk/testing.py (MockCostModel, StrategyTestHarness)
  - sdk/nexus_sdk/__init__.py (exports, version, importability)

These tests ensure the coverage source path mapping fix is definitive and that
all recently changed code paths are exercised with edge cases, boundary values,
error conditions, and property-based invariants.
"""

from __future__ import annotations

import math
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from nexus_sdk import (
    CostBreakdown,
    DataFeed,
    FactorDirection,
    FactorScore,
    IScoringStrategy,
    IStrategy,
    MarketState,
    Money,
    PortfolioSnapshot,
    ScoringFactor,
    ScoringResult,
    Side,
    Signal,
    SignalStrength,
    StrategyConfig,
    SymbolScore,
    ZScoreNormalizer,
    __all__,
    __version__,
)
from nexus_sdk.testing import MockCostModel, StrategyTestHarness
from pydantic import ValidationError

PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


class TestCoverageConfigurationRegression:
    def test_pyproject_exists(self):
        assert PYPROJECT_PATH.is_file()

    def test_coverage_source_paths_match_actual_dirs(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        sources = cfg["tool"]["coverage"]["run"]["source"]
        for src in sources:
            assert (Path(__file__).resolve().parent.parent / src).is_dir(), (
                f"coverage source '{src}' does not exist"
            )

    def test_coverage_source_includes_engine(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        assert "engine" in cfg["tool"]["coverage"]["run"]["source"]

    def test_coverage_source_includes_sdk_nexus_sdk(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        assert "sdk/nexus_sdk" in cfg["tool"]["coverage"]["run"]["source"]

    def test_coverage_source_not_bare_nexus_sdk(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        assert "nexus_sdk" not in cfg["tool"]["coverage"]["run"]["source"]

    def test_coverage_omits_tests(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        assert "tests/*" in cfg["tool"]["coverage"]["run"]["omit"]

    def test_pytest_addopts_includes_cov_engine_and_nexus_sdk(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        addopts = cfg["tool"]["pytest"]["ini_options"]["addopts"]
        assert "--cov=engine" in addopts
        assert "--cov=nexus_sdk" in addopts

    def test_pytest_pythonpath_includes_sdk(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        pythonpath = cfg["tool"]["pytest"]["ini_options"]["pythonpath"]
        assert "sdk" in pythonpath

    def test_coverage_fail_under_threshold(self):
        with PYPROJECT_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        assert cfg["tool"]["coverage"]["report"]["fail_under"] >= 80


class TestSDKPackageIntegrity:
    def test_version_string(self):
        assert __version__ == "0.1.0"

    def test_all_exports_importable(self):
        import nexus_sdk

        for name in __all__:
            assert hasattr(nexus_sdk, name), f"__all__ lists '{name}' but it is not importable"

    def test_all_exports_are_classes(self):
        expected_classes = {
            "CostBreakdown", "DataFeed", "FactorDirection", "FactorScore",
            "IScoringStrategy", "IStrategy", "MarketState", "Money",
            "PortfolioSnapshot", "ScoringFactor", "ScoringResult", "Side",
            "Signal", "SignalStrength", "StrategyConfig", "SymbolScore",
            "ZScoreNormalizer",
        }
        assert set(__all__) == expected_classes

    def test_no_shadowing_local_nexus_sdk(self):
        import sys

        mod = sys.modules.get("nexus_sdk")
        assert mod is not None
        mod_file = getattr(mod, "__file__", "")
        assert "sdk/nexus_sdk" in mod_file, f"nexus_sdk loaded from unexpected path: {mod_file}"

    def test_submodule_imports_resolve_correctly(self):
        from nexus_sdk import scoring as s
        from nexus_sdk import signals as sig
        from nexus_sdk import strategy as strat
        from nexus_sdk import testing as test_mod
        from nexus_sdk import types as t

        assert hasattr(t, "Money")
        assert hasattr(s, "ZScoreNormalizer")
        assert hasattr(sig, "Signal")
        assert hasattr(strat, "IStrategy")
        assert hasattr(test_mod, "StrategyTestHarness")


class TestMoneyEdgeCasesComprehensive:
    def test_as_pct_of_exactly_one(self):
        m = Money(amount=50.0)
        assert m.as_pct_of(50.0) == 100.0

    def test_as_pct_of_very_small_nonzero_total(self):
        m = Money(amount=1.0)
        result = m.as_pct_of(1e-10)
        assert result == pytest.approx(1e12, rel=1e-6)

    def test_as_pct_of_scientific_notation_values(self):
        m = Money(amount=5e8)
        assert m.as_pct_of(1e9) == 50.0

    def test_dataclass_equality(self):
        m1 = Money(amount=100.0, currency="USD")
        m2 = Money(amount=100.0, currency="USD")
        assert m1 == m2

    def test_dataclass_inequality_amount(self):
        m1 = Money(amount=100.0)
        m2 = Money(amount=200.0)
        assert m1 != m2

    def test_dataclass_inequality_currency(self):
        m1 = Money(amount=100.0, currency="USD")
        m2 = Money(amount=100.0, currency="EUR")
        assert m1 != m2

    def test_as_pct_of_rounding_precision(self):
        m = Money(amount=1.0 / 3.0)
        result = m.as_pct_of(1.0)
        assert abs(result - (100.0 / 3.0)) < 1e-10

    def test_zero_amount_as_pct_of_nonzero(self):
        m = Money(amount=0.0)
        assert m.as_pct_of(999.0) == 0.0

    def test_float_precision_large_amount(self):
        m = Money(amount=1e15)
        result = m.as_pct_of(2e15)
        assert result == 50.0

    def test_negative_total_returns_negative_pct(self):
        m = Money(amount=10.0)
        assert m.as_pct_of(-20.0) == -50.0


class TestCostBreakdownComprehensive:
    def test_mixed_currency_error_includes_all_currencies(self):
        cb = CostBreakdown(
            commission=Money(1.0, "USD"),
            spread=Money(2.0, "EUR"),
            slippage=Money(3.0, "GBP"),
            exchange_fee=Money(4.0, "JPY"),
            tax_estimate=Money(5.0, "CHF"),
        )
        with pytest.raises(ValueError, match="different currencies") as exc_info:
            _ = cb.total
        msg = str(exc_info.value)
        for currency in ("USD", "EUR", "GBP", "JPY", "CHF"):
            assert currency in msg

    def test_total_with_all_same_currency_non_usd(self):
        cb = CostBreakdown(
            commission=Money(10.0, "EUR"),
            spread=Money(20.0, "EUR"),
            slippage=Money(30.0, "EUR"),
            exchange_fee=Money(40.0, "EUR"),
            tax_estimate=Money(50.0, "EUR"),
        )
        total = cb.total
        assert total.amount == 150.0
        assert total.currency == "EUR"

    def test_total_with_two_currencies_raises(self):
        cb = CostBreakdown(
            commission=Money(10.0, "USD"),
            spread=Money(20.0, "USD"),
            slippage=Money(30.0, "EUR"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_total_computed_each_call(self):
        cb = CostBreakdown(commission=Money(10.0))
        assert cb.total.amount == 10.0
        cb.commission = Money(20.0)
        assert cb.total.amount == 20.0

    def test_field_default_factory_creates_independent_instances(self):
        cb1 = CostBreakdown()
        cb2 = CostBreakdown()
        cb1.commission = Money(999.0)
        assert cb2.commission.amount == 0.0

    def test_total_with_float_precision(self):
        cb = CostBreakdown(
            commission=Money(0.1),
            spread=Money(0.2),
            slippage=Money(0.3),
        )
        assert abs(cb.total.amount - 0.6) < 1e-10


class TestPortfolioSnapshotComprehensive:
    def test_allocation_weight_all_in_one(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"market_value": 100_000.0}},
        )
        assert snap.allocation_weight("AAPL") == 1.0

    def test_allocation_weight_with_negative_total_value(self):
        snap = PortfolioSnapshot(
            total_value=-100_000.0,
            positions={"AAPL": {"market_value": 25_000.0}},
        )
        weight = snap.allocation_weight("AAPL")
        assert weight == pytest.approx(-0.25)

    def test_summary_with_many_positions(self):
        positions = {f"S{i:02d}": {"qty": 1} for i in range(100)}
        snap = PortfolioSnapshot(
            cash=1_000_000.0,
            total_value=2_000_000.0,
            positions=positions,
        )
        s = snap.summary()
        assert "Positions: 100" in s

    def test_has_position_with_falsy_value(self):
        snap = PortfolioSnapshot(
            total_value=100.0,
            positions={"AAPL": {"market_value": 0.0}},
        )
        assert snap.has_position("AAPL") is True

    def test_get_position_returns_none_for_missing(self):
        snap = PortfolioSnapshot(positions={"AAPL": {"qty": 1}})
        assert snap.get_position("MSFT") is None

    def test_pydantic_model_serialization(self):
        snap = PortfolioSnapshot(
            cash=10_000.0,
            total_value=50_000.0,
            positions={"AAPL": {"qty": 100, "market_value": 15_000.0}},
            realized_pnl=5_000.0,
            unrealized_pnl=-1_000.0,
        )
        data = snap.model_dump()
        assert data["cash"] == 10_000.0
        assert data["total_value"] == 50_000.0
        assert data["realized_pnl"] == 5_000.0
        assert "AAPL" in data["positions"]

    def test_pydantic_model_json_round_trip(self):
        snap = PortfolioSnapshot(
            cash=10_000.0,
            total_value=50_000.0,
            positions={"AAPL": {"qty": 100}},
        )
        json_str = snap.model_dump_json()
        restored = PortfolioSnapshot.model_validate_json(json_str)
        assert restored.cash == snap.cash
        assert restored.total_value == snap.total_value
        assert restored.positions == snap.positions


class TestSignalFactoryComprehensive:
    def test_buy_factory_with_all_kwargs(self):
        sig = Signal.buy(
            "AAPL",
            strategy_id="strat_1",
            weight=0.8,
            quantity=100,
            strength=SignalStrength.STRONG,
            reason="Earnings beat",
            stop_loss_pct=0.05,
            take_profit_pct=0.10,
            max_cost_pct=0.02,
            metadata={"source": "test"},
        )
        assert sig.symbol == "AAPL"
        assert sig.side == Side.BUY
        assert sig.strategy_id == "strat_1"
        assert sig.weight == 0.8
        assert sig.quantity == 100
        assert sig.strength == SignalStrength.STRONG
        assert sig.reason == "Earnings beat"
        assert sig.stop_loss_pct == 0.05
        assert sig.take_profit_pct == 0.10
        assert sig.max_cost_pct == 0.02
        assert sig.metadata == {"source": "test"}

    def test_sell_factory_with_all_kwargs(self):
        sig = Signal.sell(
            "MSFT",
            strategy_id="strat_2",
            weight=0.5,
            quantity=50,
            strength=SignalStrength.WEAK,
            reason="Stop loss hit",
            metadata={"alert": "price_drop"},
        )
        assert sig.side == Side.SELL
        assert sig.strength == SignalStrength.WEAK
        assert sig.quantity == 50

    def test_hold_factory_with_all_kwargs(self):
        sig = Signal.hold(
            "GOOGL",
            strategy_id="strat_3",
            reason="Waiting for confirmation",
        )
        assert sig.side == Side.HOLD
        assert sig.reason == "Waiting for confirmation"

    def test_signal_auto_generates_id(self):
        s1 = Signal.buy("AAPL")
        s2 = Signal.buy("AAPL")
        assert s1.id != s2.id

    def test_signal_auto_generates_timestamp(self):
        before = datetime.now(UTC)
        sig = Signal.buy("AAPL")
        after = datetime.now(UTC)
        assert before <= sig.timestamp <= after

    def test_signal_default_strength_is_moderate(self):
        sig = Signal.buy("AAPL")
        assert sig.strength == SignalStrength.MODERATE

    def test_signal_default_weight_is_one(self):
        sig = Signal.buy("AAPL")
        assert sig.weight == 1.0

    def test_signal_weight_boundary_zero(self):
        sig = Signal.buy("AAPL", weight=0.0)
        assert sig.weight == 0.0

    def test_signal_weight_exceeds_one_rejected(self):
        with pytest.raises(ValidationError):
            Signal.buy("AAPL", weight=1.1)

    def test_signal_negative_weight_rejected(self):
        with pytest.raises(ValidationError):
            Signal.buy("AAPL", weight=-0.1)

    def test_side_enum_values(self):
        assert Side.BUY.value == "buy"
        assert Side.SELL.value == "sell"
        assert Side.HOLD.value == "hold"

    def test_signal_strength_enum_values(self):
        assert SignalStrength.STRONG.value == "strong"
        assert SignalStrength.MODERATE.value == "moderate"
        assert SignalStrength.WEAK.value == "weak"

    def test_signal_pydantic_serialization(self):
        sig = Signal.buy(
            "AAPL",
            strategy_id="test",
            quantity=100,
            stop_loss_pct=0.05,
        )
        data = sig.model_dump()
        assert data["symbol"] == "AAPL"
        assert data["side"] == Side.BUY
        assert data["quantity"] == 100
        assert data["stop_loss_pct"] == 0.05

    def test_signal_json_round_trip(self):
        sig = Signal.buy("AAPL", strategy_id="test")
        json_str = sig.model_dump_json()
        restored = Signal.model_validate_json(json_str)
        assert restored.symbol == sig.symbol
        assert restored.side == sig.side
        assert restored.strategy_id == sig.strategy_id


class TestScoringModelsComprehensive:
    def test_scoring_factor_validation(self):
        with pytest.raises(ValidationError):
            ScoringFactor(name="test", weight=-0.01)
        with pytest.raises(ValidationError):
            ScoringFactor(name="test", weight=1.01)

    def test_factor_direction_enum(self):
        assert FactorDirection.HIGHER_IS_BETTER.value == "higher_is_better"
        assert FactorDirection.LOWER_IS_BETTER.value == "lower_is_better"

    def test_symbol_score_clamp_exact_boundaries(self):
        ss0 = SymbolScore(symbol="X", composite_score=0.0)
        assert ss0.composite_score == 0.0
        ss100 = SymbolScore(symbol="X", composite_score=100.0)
        assert ss100.composite_score == 100.0
        ss_above = SymbolScore(symbol="X", composite_score=150.0)
        assert ss_above.composite_score == 100.0
        ss_below = SymbolScore(symbol="X", composite_score=-50.0)
        assert ss_below.composite_score == 0.0

    def test_scoring_result_sorting_preserves_order(self):
        scores = [
            SymbolScore(symbol="C", composite_score=30.0),
            SymbolScore(symbol="A", composite_score=90.0),
            SymbolScore(symbol="B", composite_score=60.0),
            SymbolScore(symbol="D", composite_score=10.0),
        ]
        result = ScoringResult(strategy_id="test", scores=scores)
        symbols = [s.symbol for s in result.scores]
        assert symbols == ["A", "B", "C", "D"]

    def test_scoring_result_rank_assignment(self):
        scores = [
            SymbolScore(symbol="X", composite_score=50.0),
            SymbolScore(symbol="Y", composite_score=80.0),
        ]
        result = ScoringResult(strategy_id="test", scores=scores)
        assert result.scores[0].rank == 1
        assert result.scores[0].symbol == "Y"
        assert result.scores[1].rank == 2
        assert result.scores[1].symbol == "X"

    def test_scoring_result_to_dict_round_trip(self):
        fs = FactorScore(factor_name="roe", z_score=1.5, raw_value=0.25)
        ss = SymbolScore(
            symbol="AAPL",
            composite_score=85.0,
            rank=1,
            factor_scores={"roe": fs},
        )
        result = ScoringResult(
            strategy_id="test",
            scores=[ss],
            excluded_factors=["momentum"],
        )
        d = result.to_dict()
        assert d["strategy_id"] == "test"
        assert len(d["scores"]) == 1
        assert d["scores"][0]["symbol"] == "AAPL"
        assert d["scores"][0]["factor_scores"]["roe"]["z_score"] == 1.5
        assert d["excluded_factors"] == ["momentum"]

    def test_factor_score_to_dict_with_none_raw(self):
        fs = FactorScore(factor_name="test", z_score=0.0)
        d = fs.to_dict()
        assert d["raw_value"] is None

    def test_symbol_score_to_dict_multiple_factors(self):
        fs1 = FactorScore(factor_name="a", z_score=1.0, raw_value=10.0)
        fs2 = FactorScore(factor_name="b", z_score=-1.0, raw_value=5.0)
        ss = SymbolScore(
            symbol="X",
            composite_score=50.0,
            rank=1,
            factor_scores={"a": fs1, "b": fs2},
        )
        d = ss.to_dict()
        assert len(d["factor_scores"]) == 2
        assert d["factor_scores"]["a"]["z_score"] == 1.0
        assert d["factor_scores"]["b"]["z_score"] == -1.0


class TestZScoreNormalizerMathProperties:
    def test_standardize_zero_mean(self):
        n = ZScoreNormalizer()
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        z = n.standardize(values)
        mean_z = sum(z) / len(z)
        assert mean_z == pytest.approx(0.0, abs=1e-10)

    def test_standardize_unit_variance(self):
        n = ZScoreNormalizer()
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        z = n.standardize(values)
        var = sum((v - sum(z) / len(z)) ** 2 for v in z) / len(z)
        assert var == pytest.approx(1.0, abs=1e-10)

    def test_standardize_preserves_order(self):
        n = ZScoreNormalizer()
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        z = n.standardize(values)
        for i in range(len(z) - 1):
            assert z[i] < z[i + 1]

    def test_scale_to_range_preserves_order(self):
        n = ZScoreNormalizer()
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        scaled = n.scale_to_range(values, low=0.0, high=100.0)
        for i in range(len(scaled) - 1):
            assert scaled[i] < scaled[i + 1]

    def test_scale_to_range_bounded(self):
        n = ZScoreNormalizer()
        values = [-100.0, 0.0, 50.0, 100.0, 1000.0]
        scaled = n.scale_to_range(values, low=0.0, high=100.0)
        assert all(0.0 <= v <= 100.0 for v in scaled)

    def test_winsorize_does_not_change_clean_data(self):
        n = ZScoreNormalizer(winsorize_lower=0.0, winsorize_upper=100.0)
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = n.winsorize(values)
        assert result == values

    def test_winsorize_clips_lower_tail(self):
        n = ZScoreNormalizer(winsorize_lower=10.0, winsorize_upper=100.0)
        values = list(range(1, 101))
        result = n.winsorize([float(v) for v in values])
        assert result[0] >= min(values)

    def test_winsorize_clips_upper_tail(self):
        n = ZScoreNormalizer(winsorize_lower=0.0, winsorize_upper=90.0)
        values = list(range(1, 101))
        result = n.winsorize([float(v) for v in values])
        assert result[-1] <= max(values)

    def test_full_pipeline_deterministic(self):
        n = ZScoreNormalizer()
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        r1 = n.winsorize_and_standardize(values)
        r2 = n.winsorize_and_standardize(values)
        assert r1 == r2

    def test_full_pipeline_with_custom_winsorize(self):
        n = ZScoreNormalizer()
        values = [1.0] * 50 + [1000.0]
        r1 = n.winsorize_and_standardize(values)
        r2 = n.winsorize_and_standardize(values, winsorize_lower=10.0, winsorize_upper=90.0)
        assert r1 != r2

    def test_standardize_large_population(self):
        n = ZScoreNormalizer()
        values = [float(i) for i in range(1000)]
        z = n.standardize(values)
        mean_z = sum(z) / len(z)
        assert mean_z == pytest.approx(0.0, abs=1e-10)

    def test_scale_to_range_negative_to_positive(self):
        n = ZScoreNormalizer()
        values = [-2.0, -1.0, 0.0, 1.0, 2.0]
        scaled = n.scale_to_range(values, low=-1.0, high=1.0)
        assert scaled[0] == pytest.approx(-1.0)
        assert scaled[-1] == pytest.approx(1.0)
        assert scaled[2] == pytest.approx(0.0)


class TestMarketStateIndicatorsComprehensive:
    def test_sma_with_exactly_period_bars(self):
        bars = [{"close": float(i)} for i in range(1, 6)]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", 5)
        assert result is not None
        expected = sum(range(1, 6)) / 5.0
        assert result == pytest.approx(expected)

    def test_sma_uses_last_n_bars(self):
        bars = [{"close": float(i)} for i in range(1, 31)]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", 3)
        assert result == pytest.approx(29.0)

    def test_std_with_known_values(self):
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        bars = [{"close": v} for v in values]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.std("AAPL", 8)
        assert result is not None
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        assert result == pytest.approx(math.sqrt(var))

    def test_std_single_value_returns_none(self):
        bars = [{"close": 42.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        assert state.std("AAPL", 2) is None

    def test_latest_returns_latest_price(self):
        state = MarketState(prices={"AAPL": 150.0, "MSFT": 300.0, "GOOGL": 2800.0})
        assert state.latest("AAPL") == 150.0
        assert state.latest("MSFT") == 300.0

    def test_latest_missing_symbol_returns_none(self):
        state = MarketState(prices={"AAPL": 150.0})
        assert state.latest("UNKNOWN") is None

    def test_get_news_returns_all(self):
        news = [{"headline": f"News {i}"} for i in range(10)]
        state = MarketState(news=news)
        assert len(state.get_news()) == 10

    def test_get_macro_returns_dict(self):
        macro = {"gdp": 2.5, "cpi": 3.1, "unemployment": 4.0}
        state = MarketState(macro=macro)
        assert state.get_macro_indicators() == macro


class TestMockCostModelComprehensive:
    def test_estimate_total_formula(self):
        model = MockCostModel(spread_bps=10.0, slippage_bps=20.0)
        price = 100.0
        qty = 50
        result = model.estimate_total("TEST", qty, price, "buy")
        expected_spread = price * (10.0 / 10_000)
        expected_slippage = price * (20.0 / 10_000) * qty
        assert result.spread.amount == pytest.approx(expected_spread)
        assert result.slippage.amount == pytest.approx(expected_slippage)

    def test_estimate_pct_formula(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("TEST", 100.0, "buy")
        expected = (5.0 + 10.0) * 2 / 10_000
        assert pct == pytest.approx(expected)

    def test_estimate_total_with_zero_quantity(self):
        model = MockCostModel()
        result = model.estimate_total("TEST", 0, 100.0, "buy")
        assert result.spread.amount > 0
        assert result.slippage.amount == 0.0

    def test_estimate_total_with_avg_volume(self):
        model = MockCostModel()
        result = model.estimate_total("TEST", 100, 50.0, "buy", avg_volume=1_000_000)
        assert isinstance(result, CostBreakdown)


class TestStrategyTestHarnessComprehensive:
    def _make_strategy_class(self):
        class _TestStrat(IStrategy):
            @property
            def id(self) -> str:
                return "test_strat"

            @property
            def name(self) -> str:
                return "Test"

            @property
            def version(self) -> str:
                return "1.0.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
                return [Signal.buy(sym, strategy_id=self.id) for sym in market.prices]

            def get_config_schema(self) -> dict:
                return {}

        return _TestStrat

    async def test_harness_full_lifecycle(self):
        cls = self._make_strategy_class()
        harness = StrategyTestHarness(cls())
        await harness.setup(params={"key": "val"})
        signals = await harness.tick(prices={"AAPL": 150.0})
        assert len(signals) == 1
        assert signals[0].side == Side.BUY
        await harness.teardown()
        assert len(harness.signals_history) == 1

    async def test_harness_multiple_ticks(self):
        cls = self._make_strategy_class()
        harness = StrategyTestHarness(cls())
        await harness.setup()
        await harness.tick(prices={"AAPL": 150.0})
        await harness.tick(prices={"AAPL": 151.0, "MSFT": 300.0})
        await harness.tick(prices={"AAPL": 152.0, "MSFT": 301.0, "GOOGL": 2800.0})
        assert len(harness.signals_history) == 3
        assert len(harness.signals_history[0]) == 1
        assert len(harness.signals_history[1]) == 2
        assert len(harness.signals_history[2]) == 3

    async def test_harness_tick_with_ohlcv_and_news(self):
        cls = self._make_strategy_class()
        harness = StrategyTestHarness(cls())
        await harness.setup()
        signals = await harness.tick(
            prices={"AAPL": 150.0},
            ohlcv={"AAPL": [{"close": 150.0}]},
            news=[{"headline": "Test"}],
        )
        assert len(signals) == 1

    async def test_assert_buy_failure_message(self):
        with pytest.raises(AssertionError, match="Expected BUY signal for MSFT"):
            StrategyTestHarness.assert_buy("MSFT", [Signal.sell("MSFT")])

    async def test_assert_sell_failure_message(self):
        with pytest.raises(AssertionError, match="Expected SELL signal for AAPL"):
            StrategyTestHarness.assert_sell("AAPL", [Signal.buy("AAPL")])

    async def test_assert_no_signals_failure_message(self):
        with pytest.raises(AssertionError, match="Expected no trade signals"):
            StrategyTestHarness.assert_no_signals([Signal.buy("AAPL")])


class TestIScoringStrategyInterface:
    def test_is_subclass_of_istrategy(self):
        assert issubclass(IScoringStrategy, IStrategy)

    def test_abstract_methods_required(self):
        with pytest.raises(TypeError):
            IScoringStrategy()

    def test_concrete_implementation(self):
        class _Concrete(IScoringStrategy):
            @property
            def id(self) -> str:
                return "concrete"

            @property
            def name(self) -> str:
                return "Concrete"

            @property
            def version(self) -> str:
                return "1.0.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
                return []

            def get_config_schema(self) -> dict:
                return {}

            def get_scoring_factors(self) -> list[ScoringFactor]:
                return [ScoringFactor(name="test", weight=1.0)]

            async def score_universe(
                self, universe: list[str], market: MarketState, costs
            ) -> ScoringResult:
                return ScoringResult(strategy_id=self.id, scores=[])

        strat = _Concrete()
        assert strat.id == "concrete"
        factors = strat.get_scoring_factors()
        assert len(factors) == 1
        assert factors[0].name == "test"


class TestStrategyConfigAndDataFeed:
    def test_strategy_config_defaults(self):
        config = StrategyConfig(strategy_id="test")
        assert config.strategy_id == "test"
        assert config.params == {}
        assert config.secrets == {}

    def test_strategy_config_with_all_fields(self):
        config = StrategyConfig(
            strategy_id="test",
            params={"threshold": 0.7, "period": 14},
            secrets={"api_key": "secret"},
        )
        assert config.params["threshold"] == 0.7
        assert config.secrets["api_key"] == "secret"

    def test_data_feed_defaults(self):
        feed = DataFeed(feed_type="ohlcv")
        assert feed.symbols == []
        assert feed.params == {}

    def test_data_feed_with_symbols(self):
        feed = DataFeed(
            feed_type="ohlcv",
            symbols=["AAPL", "MSFT"],
            params={"interval": "1d"},
        )
        assert len(feed.symbols) == 2


class TestEdgeCasesBoundaryValues:
    def test_money_as_pct_of_total_equals_amount(self):
        m = Money(amount=100.0)
        assert m.as_pct_of(100.0) == 100.0

    def test_money_as_pct_of_double_total(self):
        m = Money(amount=50.0)
        assert m.as_pct_of(100.0) == 50.0

    def test_cost_breakdown_total_with_all_zeros(self):
        cb = CostBreakdown()
        assert cb.total.amount == 0.0
        assert cb.total.currency == "USD"

    def test_portfolio_snapshot_negative_values(self):
        snap = PortfolioSnapshot(
            cash=-1000.0,
            total_value=-5000.0,
            realized_pnl=-2000.0,
            unrealized_pnl=-3000.0,
            day_pnl=-100.0,
            total_return_pct=-15.5,
        )
        assert snap.cash == -1000.0
        assert snap.total_value == -5000.0

    def test_zscore_normalizer_winsorize_empty_input(self):
        n = ZScoreNormalizer()
        assert n.winsorize([]) == []

    def test_zscore_normalizer_standardize_empty_input(self):
        n = ZScoreNormalizer()
        assert n.standardize([]) == []

    def test_zscore_normalizer_scale_empty_input(self):
        n = ZScoreNormalizer()
        assert n.scale_to_range([]) == []

    def test_zscore_normalizer_all_none_input(self):
        n = ZScoreNormalizer()
        assert n.winsorize([None, None, None]) == []

    def test_zscore_normalizer_single_value(self):
        n = ZScoreNormalizer()
        assert n.winsorize([42.0]) == [42.0]
        assert n.standardize([42.0]) == [0.0]
        assert n.scale_to_range([42.0]) == [50.0]

    def test_zscore_normalizer_two_values(self):
        n = ZScoreNormalizer()
        result = n.winsorize([1.0, 100.0])
        assert len(result) == 2

    def test_scoring_result_empty_scores(self):
        result = ScoringResult(strategy_id="test")
        assert result.scores == []
        assert result.excluded_factors == []

    def test_symbol_score_default_values(self):
        ss = SymbolScore(symbol="TEST")
        assert ss.composite_score == 0.0
        assert ss.rank == 0
        assert ss.factor_scores == {}

    def test_signal_defaults(self):
        sig = Signal(symbol="AAPL", side=Side.BUY)
        assert sig.strategy_id == ""
        assert sig.quantity is None
        assert sig.stop_loss_pct is None
        assert sig.take_profit_pct is None
        assert sig.max_cost_pct is None
        assert sig.metadata == {}
        assert sig.reason == ""

    def test_market_state_defaults(self):
        state = MarketState()
        assert state.timestamp is None
        assert state.prices == {}
        assert state.volumes == {}
        assert state.ohlcv == {}
        assert state.news == []
        assert state.sentiment == {}
        assert state.macro == {}
        assert state.order_book == {}
