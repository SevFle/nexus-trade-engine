"""Tests for Multi-Factor Z-Score Scoring Engine.

Tests the ZScoreNormalizer, scoring models, IScoringStrategy interface,
ScoringExecutor, and API endpoints.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from nexus_sdk.scoring import (
    FactorDirection,
    FactorScore,
    IScoringStrategy,
    ScoringFactor,
    ScoringResult,
    SymbolScore,
    ZScoreNormalizer,
)
from nexus_sdk.signals import Signal
from nexus_sdk.strategy import MarketState, StrategyConfig


@pytest.fixture
def normalizer() -> ZScoreNormalizer:
    return ZScoreNormalizer()


@pytest.fixture
def sample_values() -> list[float]:
    return [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]


@pytest.fixture
def factors() -> list[ScoringFactor]:
    return [
        ScoringFactor(name="roe", weight=0.5, direction=FactorDirection.HIGHER_IS_BETTER),
        ScoringFactor(name="pe_ratio", weight=0.3, direction=FactorDirection.LOWER_IS_BETTER),
        ScoringFactor(name="momentum", weight=0.2, direction=FactorDirection.HIGHER_IS_BETTER),
    ]


class _DummyScoringStrategy(IScoringStrategy):
    @property
    def id(self) -> str:
        return "dummy_scoring"

    @property
    def name(self) -> str:
        return "Dummy Scoring"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, _portfolio, _market: MarketState, _costs) -> list[Signal]:
        return []

    def get_config_schema(self) -> dict:
        return {}

    def get_scoring_factors(self) -> list[ScoringFactor]:
        return [
            ScoringFactor(name="roe", weight=0.5, direction=FactorDirection.HIGHER_IS_BETTER),
            ScoringFactor(name="pe_ratio", weight=0.3, direction=FactorDirection.LOWER_IS_BETTER),
            ScoringFactor(name="momentum", weight=0.2, direction=FactorDirection.HIGHER_IS_BETTER),
        ]

    async def score_universe(
        self, _universe: list[str], _market: MarketState, _costs
    ) -> ScoringResult:
        return ScoringResult(strategy_id=self.id, scores=[])


class TestZScoreNormalizerWinsorize:
    def test_caps_at_default_percentiles(self, normalizer, sample_values):
        result = normalizer.winsorize(sample_values)
        assert result[0] == pytest.approx(1.0, abs=0.01)
        assert result[-1] == pytest.approx(10.0, abs=0.01)

    def test_caps_extreme_values(self, normalizer):
        values = [*list(range(1, 101)), 10000.0]
        result = normalizer.winsorize(values)
        assert result[-1] < 10000.0

    def test_custom_percentiles(self):
        normalizer = ZScoreNormalizer(winsorize_lower=5, winsorize_upper=95)
        values = [*list(range(1, 101)), 10000.0]
        result = normalizer.winsorize(values)
        assert result[-1] < 10000.0

    def test_single_value_returns_same(self, normalizer):
        result = normalizer.winsorize([42.0])
        assert result == [42.0]

    def test_empty_returns_empty(self, normalizer):
        assert normalizer.winsorize([]) == []

    def test_none_values_are_filtered(self, normalizer):
        values = [1.0, None, 3.0, None, 5.0]
        result = normalizer.winsorize(values)
        assert len(result) == 3
        assert None not in result


class TestZScoreNormalizerStandardize:
    def test_produces_zero_mean_unit_var(self, normalizer, sample_values):
        z_scores = normalizer.standardize(sample_values)
        mean_z = sum(z_scores) / len(z_scores)
        assert mean_z == pytest.approx(0.0, abs=0.01)

    def test_single_value_returns_zero(self, normalizer):
        result = normalizer.standardize([5.0])
        assert result == [0.0]

    def test_empty_returns_empty(self, normalizer):
        assert normalizer.standardize([]) == []

    def test_uniform_values_return_zeros(self, normalizer):
        result = normalizer.standardize([5.0, 5.0, 5.0])
        assert all(z == 0.0 for z in result)


class TestZScoreNormalizerScaleToRange:
    def test_scales_to_0_100(self, normalizer):
        z_scores = [-2.0, -1.0, 0.0, 1.0, 2.0]
        scaled = normalizer.scale_to_range(z_scores, low=0.0, high=100.0)
        assert scaled[0] == pytest.approx(0.0, abs=0.01)
        assert scaled[-1] == pytest.approx(100.0, abs=0.01)

    def test_single_value_returns_midpoint(self, normalizer):
        result = normalizer.scale_to_range([0.0])
        assert result == [50.0]


class TestZScoreNormalizerFullPipeline:
    def test_winsorize_then_standardize(self, normalizer):
        values = [1.0] * 5 + [100.0]
        z_scores = normalizer.winsorize_and_standardize(values)
        assert all(isinstance(z, float) for z in z_scores)
        assert len(z_scores) == len(values)


class TestScoringFactor:
    def test_creates_factor_with_defaults(self):
        factor = ScoringFactor(name="roe", weight=0.5)
        assert factor.name == "roe"
        assert factor.weight == 0.5
        assert factor.direction == FactorDirection.HIGHER_IS_BETTER
        assert factor.winsorize_pct == (1, 99)

    def test_creates_factor_with_custom_winsorize(self):
        factor = ScoringFactor(name="pe", weight=0.3, winsorize_pct=(2, 98))
        assert factor.winsorize_pct == (2, 98)

    def test_lower_is_better_direction(self):
        factor = ScoringFactor(
            name="debt_ratio", weight=0.4, direction=FactorDirection.LOWER_IS_BETTER
        )
        assert factor.direction == FactorDirection.LOWER_IS_BETTER


class TestFactorScore:
    def test_creates_factor_score(self):
        fs = FactorScore(factor_name="roe", z_score=1.5, raw_value=0.25)
        assert fs.factor_name == "roe"
        assert fs.z_score == 1.5
        assert fs.raw_value == 0.25


class TestSymbolScore:
    def test_creates_symbol_score(self):
        fs = FactorScore(factor_name="roe", z_score=1.5, raw_value=0.25)
        ss = SymbolScore(symbol="AAPL", composite_score=85.0, rank=1, factor_scores={"roe": fs})
        assert ss.symbol == "AAPL"
        assert ss.composite_score == 85.0
        assert ss.rank == 1

    def test_composite_score_bounded(self):
        ss = SymbolScore(symbol="AAPL", composite_score=105.0, rank=1, factor_scores={})
        assert ss.composite_score <= 100.0

    def test_composite_score_min_zero(self):
        ss = SymbolScore(symbol="AAPL", composite_score=-5.0, rank=1, factor_scores={})
        assert ss.composite_score >= 0.0


class TestScoringResult:
    def test_sorted_by_composite_desc(self):
        scores = [
            SymbolScore(symbol="B", composite_score=50.0, rank=2, factor_scores={}),
            SymbolScore(symbol="A", composite_score=90.0, rank=1, factor_scores={}),
            SymbolScore(symbol="C", composite_score=30.0, rank=3, factor_scores={}),
        ]
        result = ScoringResult(strategy_id="test", scores=scores)
        assert result.scores[0].symbol == "A"
        assert result.scores[1].symbol == "B"
        assert result.scores[2].symbol == "C"

    def test_rank_is_set(self):
        scores = [
            SymbolScore(symbol="C", composite_score=30.0, rank=3, factor_scores={}),
            SymbolScore(symbol="A", composite_score=90.0, rank=1, factor_scores={}),
        ]
        result = ScoringResult(strategy_id="test", scores=scores)
        assert result.scores[0].rank == 1
        assert result.scores[1].rank == 2


class TestIScoringStrategy:
    def test_is_subclass_of_istrategy_interface(self):
        from nexus_sdk.strategy import IStrategy

        assert issubclass(IScoringStrategy, IStrategy)

    @pytest.mark.asyncio
    async def test_concrete_strategy_instantiates(self):
        strategy = _DummyScoringStrategy()
        assert strategy.id == "dummy_scoring"
        factors = strategy.get_scoring_factors()
        assert len(factors) == 3
        assert sum(f.weight for f in factors) == pytest.approx(1.0)


class TestScoringExecutor:
    def test_executor_runs_scoring(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _DummyScoringStrategy()
        executor = ScoringExecutor(strategy, min_data_points=2)
        universe = ["AAPL", "MSFT", "GOOGL"]
        market = MarketState(
            prices={"AAPL": 150.0, "MSFT": 300.0, "GOOGL": 2800.0},
        )
        result = executor.execute(universe, market, None)
        assert result is not None
        assert result.strategy_id == "dummy_scoring"

    def test_executor_compute_scores_single_factor(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        class _PriceScoringStrat(IScoringStrategy):
            @property
            def id(self) -> str:
                return "price_scoring"

            @property
            def name(self) -> str:
                return "Price Scoring"

            @property
            def version(self) -> str:
                return "0.1.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, _portfolio, _market: MarketState, _costs) -> list[Signal]:
                return []

            def get_config_schema(self) -> dict:
                return {}

            def get_scoring_factors(self) -> list[ScoringFactor]:
                return [
                    ScoringFactor(
                        name="price", weight=1.0, direction=FactorDirection.HIGHER_IS_BETTER
                    ),
                ]

            async def score_universe(
                self, _universe: list[str], _market: MarketState, _costs
            ) -> ScoringResult:
                return ScoringResult(strategy_id=self.id, scores=[])

        strategy = _PriceScoringStrat()
        executor = ScoringExecutor(strategy, min_data_points=2)

        raw_data = {
            "AAPL": {"price": 150.0},
            "MSFT": {"price": 300.0},
            "GOOGL": {"price": 2800.0},
        }
        result = executor.compute_scores(["AAPL", "MSFT", "GOOGL"], raw_data)
        assert result is not None
        assert len(result.scores) == 3
        assert result.scores[0].symbol == "GOOGL"
        assert result.scores[0].rank == 1
        assert result.scores[0].composite_score > result.scores[1].composite_score

    def test_executor_lower_is_better(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _DummyScoringStrategy()
        executor = ScoringExecutor(strategy, min_data_points=2)

        raw_data = {
            "AAPL": {"roe": 0.25, "pe_ratio": 30.0, "momentum": 0.1},
            "MSFT": {"roe": 0.20, "pe_ratio": 15.0, "momentum": 0.05},
            "GOOGL": {"roe": 0.15, "pe_ratio": 25.0, "momentum": 0.08},
        }
        result = executor.compute_scores(["AAPL", "MSFT", "GOOGL"], raw_data)
        assert len(result.scores) == 3
        for score in result.scores:
            assert 0.0 <= score.composite_score <= 100.0

    def test_executor_handles_missing_data(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _DummyScoringStrategy()
        executor = ScoringExecutor(strategy, min_data_points=2)

        raw_data = {
            "AAPL": {"roe": 0.25, "pe_ratio": 30.0},
        }
        result = executor.compute_scores(["AAPL", "UNKNOWN"], raw_data)
        assert len(result.scores) <= 2

    def test_executor_excludes_factors_with_insufficient_data(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _DummyScoringStrategy()
        executor = ScoringExecutor(strategy, min_data_points=2)

        raw_data = {
            "AAPL": {"roe": 0.25, "pe_ratio": 30.0, "momentum": 0.1},
            "MSFT": {"roe": None, "pe_ratio": 15.0, "momentum": None},
        }
        result = executor.compute_scores(["AAPL", "MSFT"], raw_data)
        assert len(result.scores) >= 1


class TestPluginRegistryScoringDetection:
    def test_detects_scoring_strategy(self, tmp_path: Path):
        import textwrap

        from engine.plugins.registry import PluginRegistry

        code = textwrap.dedent("""\
            from nexus_sdk.scoring import IScoringStrategy, ScoringFactor, ScoringResult
            from nexus_sdk.strategy import StrategyConfig, MarketState
            from nexus_sdk.signals import Signal

            class Strategy(IScoringStrategy):
                @property
                def id(self):
                    return "test_scoring"

                @property
                def name(self):
                    return "test_scoring"

                @property
                def version(self):
                    return "0.1.0"

                async def initialize(self, config):
                    pass

                async def dispose(self):
                    pass

                async def evaluate(self, portfolio, market, costs):
                    return []

                def get_config_schema(self):
                    return {}

                def get_scoring_factors(self):
                    return [ScoringFactor(name="test", weight=1.0)]

                async def score_universe(self, universe, market, costs):
                    return ScoringResult(strategy_id=self.id, scores=[])
        """)
        strat_dir = tmp_path / "test_scoring"
        strat_dir.mkdir()
        (strat_dir / "strategy.py").write_text(code)
        manifest = {"name": "test_scoring", "version": "0.1.0"}
        with (strat_dir / "manifest.yaml").open("w") as f:
            yaml.dump(manifest, f)

        registry = PluginRegistry(tmp_path)
        instance = registry.load_strategy("test_scoring")
        assert instance is not None
        assert isinstance(instance, IScoringStrategy)

    def test_is_scoring_strategy_flag(self):
        from engine.plugins.registry import is_scoring_strategy

        strategy = _DummyScoringStrategy()
        assert is_scoring_strategy(strategy) is True

    def test_regular_strategy_not_flagged(self):
        from engine.plugins.registry import is_scoring_strategy

        class _RegularStrategy:
            pass

        assert is_scoring_strategy(_RegularStrategy()) is False


class TestScoringResultSerialization:
    def test_to_dict(self):
        fs = FactorScore(factor_name="roe", z_score=1.5, raw_value=0.25)
        ss = SymbolScore(symbol="AAPL", composite_score=85.0, rank=1, factor_scores={"roe": fs})
        result = ScoringResult(strategy_id="test", scores=[ss])

        data = result.to_dict()
        assert data["strategy_id"] == "test"
        assert len(data["scores"]) == 1
        assert data["scores"][0]["symbol"] == "AAPL"
        assert data["scores"][0]["composite_score"] == 85.0
        assert data["scores"][0]["factor_scores"]["roe"]["z_score"] == 1.5


class TestZScoreNormalizerEdgeCases:
    def test_large_universe(self):
        import random

        normalizer = ZScoreNormalizer()
        random.seed(42)
        values = [random.gauss(50, 15) for _ in range(500)]
        z_scores = normalizer.winsorize_and_standardize(values)
        assert len(z_scores) == 500
        mean_z = sum(z_scores) / len(z_scores)
        assert mean_z == pytest.approx(0.0, abs=0.1)

    def test_many_outliers(self):
        normalizer = ZScoreNormalizer()
        values = [10.0] * 100 + [10000.0] * 5
        z_scores = normalizer.winsorize_and_standardize(values)
        assert max(z_scores) < 5.0

    def test_negative_values(self):
        normalizer = ZScoreNormalizer()
        values = [-5.0, -3.0, -1.0, 1.0, 3.0, 5.0]
        z_scores = normalizer.winsorize_and_standardize(values)
        assert len(z_scores) == 6
        mean_z = sum(z_scores) / len(z_scores)
        assert mean_z == pytest.approx(0.0, abs=0.01)

    def test_all_same_values(self):
        normalizer = ZScoreNormalizer()
        values = [42.0] * 10
        z_scores = normalizer.winsorize_and_standardize(values)
        assert all(z == 0.0 for z in z_scores)
