"""
Integration tests for nexus_sdk — validates cross-module workflows,
SDK-level contracts, and edge cases for SEV-264 coverage verification.

These tests exercise the recently changed types.py, scoring.py, strategy.py,
signals.py, and testing.py modules working together.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

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
)
from nexus_sdk.testing import MockCostModel, StrategyTestHarness


class _MomentumStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "momentum_v1"

    @property
    def name(self) -> str:
        return "Momentum Strategy"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def author(self) -> str:
        return "test_author"

    @property
    def description(self) -> str:
        return "Buys on positive momentum, sells on negative"

    async def initialize(self, config: StrategyConfig) -> None:
        self._config = config

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        signals = []
        for symbol, price in market.prices.items():
            sma = market.sma(symbol, 3)
            if sma is None:
                continue
            if price > sma * 1.01:
                signals.append(
                    Signal.buy(symbol, strategy_id=self.id, weight=0.8)
                )
            elif price < sma * 0.99:
                signals.append(
                    Signal.sell(symbol, strategy_id=self.id, weight=0.8)
                )
            else:
                signals.append(
                    Signal.hold(symbol, strategy_id=self.id)
                )
        return signals

    def get_config_schema(self) -> dict:
        return {"type": "object", "properties": {"threshold": {"type": "number"}}}


class _UniverseScorer(IScoringStrategy):
    def __init__(self):
        self._factors = [
            ScoringFactor(name="value", weight=0.4, direction=FactorDirection.LOWER_IS_BETTER),
            ScoringFactor(name="quality", weight=0.3, direction=FactorDirection.HIGHER_IS_BETTER),
            ScoringFactor(name="momentum", weight=0.3, direction=FactorDirection.HIGHER_IS_BETTER),
        ]

    @property
    def id(self) -> str:
        return "universe_scorer"

    @property
    def name(self) -> str:
        return "Universe Scorer"

    @property
    def version(self) -> str:
        return "2.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        return []

    def get_config_schema(self) -> dict:
        return {}

    def get_scoring_factors(self) -> list[ScoringFactor]:
        return self._factors

    async def score_universe(
        self, universe: list[str], market: MarketState, costs
    ) -> ScoringResult:
        scores = []
        normalizer = ZScoreNormalizer()
        for symbol in universe:
            price = market.latest(symbol)
            if price is None:
                continue
            quality_raw = price / 100.0
            momentum_raw = price * 0.5
            composite = quality_raw * 0.5 + momentum_raw * 0.5
            score = SymbolScore(
                symbol=symbol,
                composite_score=min(100.0, max(0.0, composite)),
                factor_scores={
                    "quality": FactorScore(factor_name="quality", z_score=quality_raw, raw_value=quality_raw),
                    "momentum": FactorScore(factor_name="momentum", z_score=momentum_raw, raw_value=momentum_raw),
                },
            )
            scores.append(score)
        return ScoringResult(strategy_id=self.id, scores=scores)


class TestSDKImportContract:
    def test_all_public_symbols_importable(self):
        import nexus_sdk

        for name in nexus_sdk.__all__:
            assert hasattr(nexus_sdk, name), f"Missing public symbol: {name}"

    def test_version_is_string(self):
        import nexus_sdk

        assert isinstance(nexus_sdk.__version__, str)
        parts = nexus_sdk.__version__.split(".")
        assert len(parts) == 3

    def test_side_enum_values(self):
        assert Side.BUY.value == "buy"
        assert Side.SELL.value == "sell"
        assert Side.HOLD.value == "hold"

    def test_signal_strength_enum_values(self):
        assert SignalStrength.STRONG.value == "strong"
        assert SignalStrength.MODERATE.value == "moderate"
        assert SignalStrength.WEAK.value == "weak"

    def test_factor_direction_enum_values(self):
        assert FactorDirection.HIGHER_IS_BETTER.value == "higher_is_better"
        assert FactorDirection.LOWER_IS_BETTER.value == "lower_is_better"


class TestSignalFactoryMethods:
    def test_buy_factory(self):
        sig = Signal.buy("AAPL", strategy_id="test")
        assert sig.symbol == "AAPL"
        assert sig.side == Side.BUY
        assert sig.strategy_id == "test"

    def test_sell_factory(self):
        sig = Signal.sell("MSFT", strategy_id="test")
        assert sig.symbol == "MSFT"
        assert sig.side == Side.SELL

    def test_hold_factory(self):
        sig = Signal.hold("GOOGL", strategy_id="test")
        assert sig.symbol == "GOOGL"
        assert sig.side == Side.HOLD

    def test_buy_with_weight_and_strength(self):
        sig = Signal.buy(
            "TSLA",
            strategy_id="strat",
            weight=0.75,
            strength=SignalStrength.STRONG,
            reason="strong momentum",
        )
        assert sig.weight == 0.75
        assert sig.strength == SignalStrength.STRONG
        assert sig.reason == "strong momentum"

    def test_signal_auto_generates_id(self):
        sig1 = Signal.buy("AAPL")
        sig2 = Signal.buy("AAPL")
        assert sig1.id != sig2.id

    def test_signal_auto_generates_timestamp(self):
        before = datetime.now(UTC)
        sig = Signal.buy("AAPL")
        after = datetime.now(UTC)
        assert before <= sig.timestamp <= after

    def test_signal_with_stop_loss(self):
        sig = Signal.buy("AAPL", stop_loss_pct=5.0, take_profit_pct=10.0, max_cost_pct=1.0)
        assert sig.stop_loss_pct == 5.0
        assert sig.take_profit_pct == 10.0
        assert sig.max_cost_pct == 1.0


class TestMoneyCostBreakdownIntegration:
    def test_cost_breakdown_total_used_as_pct(self):
        cb = CostBreakdown(
            commission=Money(5.0),
            spread=Money(2.0),
            slippage=Money(3.0),
        )
        total = cb.total
        pct = total.as_pct_of(1000.0)
        assert pct == 1.0

    def test_portfolio_with_cost_tracking(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            total_value=100_000.0,
        )
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        cost = model.estimate_total("AAPL", 100, 150.0, "buy")
        assert cost.spread.amount > 0
        assert cost.slippage.amount > 0
        assert snap.cash == 100_000.0

    def test_mock_cost_model_pct_matches_total(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", 100.0)
        assert pct == (5.0 + 10.0) * 2 / 10_000


class TestStrategyFullLifecycle:
    @pytest.fixture
    def momentum_strategy(self):
        return _MomentumStrategy()

    @pytest.fixture
    def harness(self, momentum_strategy):
        return StrategyTestHarness(momentum_strategy, initial_cash=50_000.0)

    async def test_initialize_dispose_lifecycle(self, harness):
        await harness.setup(params={"threshold": 0.02})
        await harness.teardown()

    async def test_buy_signal_when_price_above_sma(self, harness):
        await harness.setup()
        ohlcv = {
            "AAPL": [
                {"close": 145.0},
                {"close": 148.0},
                {"close": 150.0},
            ]
        }
        signals = await harness.tick(prices={"AAPL": 155.0}, ohlcv=ohlcv)
        StrategyTestHarness.assert_buy("AAPL", signals)

    async def test_sell_signal_when_price_below_sma(self, harness):
        await harness.setup()
        ohlcv = {
            "AAPL": [
                {"close": 155.0},
                {"close": 152.0},
                {"close": 150.0},
            ]
        }
        signals = await harness.tick(prices={"AAPL": 145.0}, ohlcv=ohlcv)
        StrategyTestHarness.assert_sell("AAPL", signals)

    async def test_hold_signal_when_price_near_sma(self, harness):
        await harness.setup()
        ohlcv = {
            "AAPL": [
                {"close": 148.0},
                {"close": 149.0},
                {"close": 150.0},
            ]
        }
        signals = await harness.tick(prices={"AAPL": 150.3}, ohlcv=ohlcv)
        StrategyTestHarness.assert_no_signals(signals)

    async def test_multi_symbol_evaluation(self, harness):
        await harness.setup()
        ohlcv = {
            "AAPL": [{"close": 148.0}, {"close": 149.0}, {"close": 150.0}],
            "MSFT": [{"close": 298.0}, {"close": 299.0}, {"close": 300.0}],
            "TSLA": [{"close": 200.0}, {"close": 195.0}, {"close": 190.0}],
        }
        signals = await harness.tick(
            prices={"AAPL": 153.0, "MSFT": 300.5, "TSLA": 185.0},
            ohlcv=ohlcv,
        )
        symbols_with_signals = {s.symbol for s in signals if s.side != Side.HOLD}
        assert "AAPL" in symbols_with_signals
        assert "TSLA" in symbols_with_signals


class TestMarketStateIndicators:
    def test_latest_returns_none_for_missing(self):
        state = MarketState()
        assert state.latest("AAPL") is None

    def test_sma_with_exact_period(self):
        bars = [{"close": float(i)} for i in range(1, 6)]
        state = MarketState(ohlcv={"AAPL": bars})
        sma = state.sma("AAPL", 5)
        assert sma is not None
        assert sma == 3.0

    def test_sma_returns_none_insufficient_data(self):
        bars = [{"close": 100.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        assert state.sma("AAPL", 20) is None

    def test_std_calculation(self):
        bars = [{"close": float(i)} for i in range(1, 6)]
        state = MarketState(ohlcv={"AAPL": bars})
        std = state.std("AAPL", 5)
        assert std is not None
        assert std > 0

    def test_std_returns_none_insufficient_data(self):
        bars = [{"close": 100.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        assert state.std("AAPL", 20) is None

    def test_get_news_returns_list(self):
        news = [{"headline": "Test"}]
        state = MarketState(news=news)
        assert state.get_news() == news

    def test_get_macro_indicators_returns_dict(self):
        macro = {"gdp": 2.5, "inflation": 3.1}
        state = MarketState(macro=macro)
        assert state.get_macro_indicators() == macro


class TestScoringPipeline:
    def test_zscore_normalizer_winsorize(self):
        norm = ZScoreNormalizer(winsorize_lower=5.0, winsorize_upper=95.0)
        values = [float(i) for i in range(100)]
        result = norm.winsorize(values)
        assert len(result) == 100
        assert min(result) >= 5.0
        assert max(result) <= 95.0

    def test_zscore_normalizer_winsorize_empty(self):
        norm = ZScoreNormalizer()
        assert norm.winsorize([]) == []

    def test_zscore_normalizer_winsorize_all_none(self):
        norm = ZScoreNormalizer()
        assert norm.winsorize([None, None, None]) == []

    def test_zscore_normalizer_winsorize_single_value(self):
        norm = ZScoreNormalizer()
        result = norm.winsorize([42.0])
        assert result == [42.0]

    def test_zscore_normalizer_winsorize_with_nones(self):
        norm = ZScoreNormalizer()
        result = norm.winsorize([1.0, None, 3.0, None, 5.0])
        assert len(result) == 3
        assert 1.0 in result
        assert 3.0 in result
        assert 5.0 in result

    def test_zscore_normalizer_standardize(self):
        norm = ZScoreNormalizer()
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = norm.standardize(values)
        assert len(result) == 5
        assert abs(sum(result)) < 1e-10

    def test_zscore_normalizer_standardize_empty(self):
        norm = ZScoreNormalizer()
        assert norm.standardize([]) == []

    def test_zscore_normalizer_standardize_single(self):
        norm = ZScoreNormalizer()
        assert norm.standardize([42.0]) == [0.0]

    def test_zscore_normalizer_standardize_constant(self):
        norm = ZScoreNormalizer()
        result = norm.standardize([5.0, 5.0, 5.0])
        assert result == [0.0, 0.0, 0.0]

    def test_zscore_normalizer_scale_to_range(self):
        norm = ZScoreNormalizer()
        values = [0.0, 1.0, 2.0, 3.0, 4.0]
        result = norm.scale_to_range(values, low=0.0, high=100.0)
        assert len(result) == 5
        assert result[0] == 0.0
        assert result[-1] == 100.0

    def test_zscore_normalizer_scale_empty(self):
        norm = ZScoreNormalizer()
        assert norm.scale_to_range([]) == []

    def test_zscore_normalizer_scale_single(self):
        norm = ZScoreNormalizer()
        result = norm.scale_to_range([42.0], low=0.0, high=100.0)
        assert result == [50.0]

    def test_zscore_normalizer_scale_constant(self):
        norm = ZScoreNormalizer()
        result = norm.scale_to_range([3.0, 3.0, 3.0], low=0.0, high=100.0)
        assert result == [50.0, 50.0, 50.0]

    def test_winsorize_and_standardize(self):
        norm = ZScoreNormalizer(winsorize_lower=5.0, winsorize_upper=95.0)
        values = [float(i) for i in range(20)]
        result = norm.winsorize_and_standardize(values)
        assert len(result) == 20
        assert abs(sum(result)) < 1.0

    def test_winsorize_and_standardize_custom_bounds(self):
        norm = ZScoreNormalizer()
        values = [float(i) for i in range(20)]
        result = norm.winsorize_and_standardize(values, winsorize_lower=10.0, winsorize_upper=90.0)
        assert len(result) == 20


class TestScoringModels:
    def test_factor_score_to_dict(self):
        fs = FactorScore(factor_name="test", z_score=1.5, raw_value=42.0)
        d = fs.to_dict()
        assert d["factor_name"] == "test"
        assert d["z_score"] == 1.5
        assert d["raw_value"] == 42.0

    def test_factor_score_raw_value_none(self):
        fs = FactorScore(factor_name="test")
        assert fs.raw_value is None
        d = fs.to_dict()
        assert d["raw_value"] is None

    def test_symbol_score_clamp_above_100(self):
        score = SymbolScore(symbol="AAPL", composite_score=150.0)
        assert score.composite_score == 100.0

    def test_symbol_score_clamp_below_0(self):
        score = SymbolScore(symbol="AAPL", composite_score=-50.0)
        assert score.composite_score == 0.0

    def test_symbol_score_to_dict(self):
        score = SymbolScore(
            symbol="AAPL",
            composite_score=75.0,
            rank=1,
            factor_scores={"test": FactorScore(factor_name="test", z_score=1.0)},
        )
        d = score.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["composite_score"] == 75.0
        assert d["rank"] == 1
        assert "test" in d["factor_scores"]

    def test_scoring_result_sorts_by_composite(self):
        s1 = SymbolScore(symbol="AAPL", composite_score=80.0)
        s2 = SymbolScore(symbol="MSFT", composite_score=90.0)
        s3 = SymbolScore(symbol="GOOGL", composite_score=70.0)
        result = ScoringResult(strategy_id="test", scores=[s1, s2, s3])
        assert result.scores[0].symbol == "MSFT"
        assert result.scores[1].symbol == "AAPL"
        assert result.scores[2].symbol == "GOOGL"
        assert result.scores[0].rank == 1
        assert result.scores[1].rank == 2
        assert result.scores[2].rank == 3

    def test_scoring_result_to_dict(self):
        s1 = SymbolScore(symbol="AAPL", composite_score=85.0)
        result = ScoringResult(
            strategy_id="test",
            scores=[s1],
            excluded_factors=["bad_factor"],
        )
        d = result.to_dict()
        assert d["strategy_id"] == "test"
        assert len(d["scores"]) == 1
        assert d["excluded_factors"] == ["bad_factor"]

    def test_scoring_result_empty_scores(self):
        result = ScoringResult(strategy_id="empty")
        assert result.scores == []
        assert result.excluded_factors == []

    def test_scoring_factor_defaults(self):
        sf = ScoringFactor(name="test", weight=0.5)
        assert sf.direction == FactorDirection.HIGHER_IS_BETTER
        assert sf.composite_fields == []
        assert sf.winsorize_pct == (1.0, 99.0)


class TestScoringStrategyIntegration:
    @pytest.fixture
    def scorer(self):
        return _UniverseScorer()

    async def test_score_universe_produces_ranked_result(self, scorer):
        market = MarketState(prices={"AAPL": 150.0, "MSFT": 300.0, "GOOGL": 100.0})
        result = await scorer.score_universe(["AAPL", "MSFT", "GOOGL"], market, None)
        assert result.strategy_id == "universe_scorer"
        assert len(result.scores) == 3
        assert result.scores[0].composite_score >= result.scores[-1].composite_score

    async def test_score_universe_excludes_missing_symbols(self, scorer):
        market = MarketState(prices={"AAPL": 150.0})
        result = await scorer.score_universe(["AAPL", "MISSING"], market, None)
        assert len(result.scores) == 1
        assert result.scores[0].symbol == "AAPL"

    def test_get_scoring_factors(self, scorer):
        factors = scorer.get_scoring_factors()
        assert len(factors) == 3
        total_weight = sum(f.weight for f in factors)
        assert abs(total_weight - 1.0) < 1e-10


class TestStrategyConfig:
    def test_config_with_params_and_secrets(self):
        config = StrategyConfig(
            strategy_id="test",
            params={"threshold": 0.5},
            secrets={"api_key": "secret"},
        )
        assert config.strategy_id == "test"
        assert config.params["threshold"] == 0.5
        assert config.secrets["api_key"] == "secret"

    def test_config_defaults(self):
        config = StrategyConfig(strategy_id="test")
        assert config.params == {}
        assert config.secrets == {}


class TestDataFeed:
    def test_datafeed_defaults(self):
        feed = DataFeed(feed_type="ohlcv")
        assert feed.symbols == []
        assert feed.params == {}

    def test_datafeed_with_symbols(self):
        feed = DataFeed(feed_type="ohlcv", symbols=["AAPL", "MSFT"])
        assert len(feed.symbols) == 2


class TestIStrategyDefaults:
    def test_default_author(self):
        class _MinimalStrategy(IStrategy):
            @property
            def id(self): return "test"
            @property
            def name(self): return "Test"
            @property
            def version(self): return "1.0"
            async def initialize(self, config): pass
            async def dispose(self): pass
            async def evaluate(self, portfolio, market, costs): return []
            def get_config_schema(self): return {}

        s = _MinimalStrategy()
        assert s.author == "unknown"
        assert s.description == ""

    async def test_default_hooks(self):
        class _MinimalStrategy(IStrategy):
            @property
            def id(self): return "test"
            @property
            def name(self): return "Test"
            @property
            def version(self): return "1.0"
            async def initialize(self, config): pass
            async def dispose(self): pass
            async def evaluate(self, portfolio, market, costs): return []
            def get_config_schema(self): return {}

        s = _MinimalStrategy()
        await s.on_order_fill({"symbol": "AAPL", "qty": 100})
        await s.on_market_open()
        await s.on_market_close()
        assert s.get_required_data_feeds() == [DataFeed(feed_type="ohlcv")]
        assert s.get_min_history_bars() == 50
        assert s.get_watchlist() == []


class TestPortfolioSnapshotEdgeCases:
    def test_allocation_weight_sum_to_one(self):
        snap = PortfolioSnapshot(
            total_value=200_000.0,
            positions={
                "AAPL": {"market_value": 80_000.0},
                "MSFT": {"market_value": 70_000.0},
                "GOOGL": {"market_value": 50_000.0},
            },
        )
        total_weight = sum(
            snap.allocation_weight(s) for s in ["AAPL", "MSFT", "GOOGL"]
        )
        assert abs(total_weight - 1.0) < 1e-10

    def test_summary_single_position(self):
        snap = PortfolioSnapshot(
            cash=90_000.0,
            total_value=100_000.0,
            positions={"AAPL": {"qty": 10, "market_value": 10_000.0}},
        )
        s = snap.summary()
        assert "Positions: 1" in s
        assert "$100,000.00" in s

    def test_snapshot_serialization_round_trip(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            total_value=150_000.0,
            positions={"AAPL": {"qty": 100}},
            realized_pnl=5_000.0,
            unrealized_pnl=-2_000.0,
            day_pnl=300.0,
            total_return_pct=3.5,
        )
        data = snap.model_dump()
        restored = PortfolioSnapshot(**data)
        assert restored.cash == snap.cash
        assert restored.total_value == snap.total_value
        assert restored.realized_pnl == snap.realized_pnl
        assert len(restored.positions) == 1
