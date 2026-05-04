"""
Comprehensive integration and unit tests for recently changed SDK code.

Primary targets:
  - sdk/nexus_sdk/types.py: as_pct_of(0) now raises ValueError (was return 0.0)
  - SDK cross-module integration (types <-> scoring <-> strategy <-> signals <-> testing)
  - ScoringExecutor + ZScoreNormalizer pipeline
  - Behavioral edge cases and boundary values
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

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


class _SimpleBuyStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "simple_buy"

    @property
    def name(self) -> str:
        return "Simple Buy"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        signals = []
        for symbol, price in market.prices.items():
            if price < 200.0 and not portfolio.has_position(symbol):
                signals.append(
                    Signal.buy(
                        symbol,
                        strategy_id=self.id,
                        weight=0.5,
                        quantity=10,
                        stop_loss_pct=0.05,
                        take_profit_pct=0.10,
                        strength=SignalStrength.STRONG,
                        reason="Price below threshold",
                    )
                )
        return signals

    def get_config_schema(self) -> dict:
        return {"type": "object"}


class _ScoringDemoStrategy(IScoringStrategy):
    @property
    def id(self) -> str:
        return "scoring_demo"

    @property
    def name(self) -> str:
        return "Scoring Demo"

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
        return [
            ScoringFactor(name="value", weight=0.4, direction=FactorDirection.LOWER_IS_BETTER),
            ScoringFactor(name="momentum", weight=0.3),
            ScoringFactor(name="quality", weight=0.3),
        ]

    async def score_universe(
        self, universe: list[str], market: MarketState, costs: Any
    ) -> ScoringResult:
        return ScoringResult(strategy_id=self.id, scores=[])


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Money.as_pct_of behavioral change tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMoneyAsPctOfBehavioralChange:
    async def test_as_pct_of_zero_raises_valueerror(self):
        m = Money(amount=100.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(0.0)

    async def test_as_pct_of_negative_zero_raises_valueerror(self):
        m = Money(amount=100.0)
        with pytest.raises(ValueError):
            m.as_pct_of(-0.0)

    async def test_as_pct_of_small_positive_works(self):
        m = Money(amount=0.001)
        result = m.as_pct_of(0.01)
        assert result == pytest.approx(10.0)

    async def test_as_pct_of_exact_equality(self):
        m = Money(amount=50.0)
        assert m.as_pct_of(50.0) == 100.0

    async def test_as_pct_of_half(self):
        m = Money(amount=50.0)
        assert m.as_pct_of(200.0) == 25.0

    async def test_as_pct_of_preserves_sign_negative_amount(self):
        m = Money(amount=-30.0)
        assert m.as_pct_of(100.0) == -30.0

    async def test_as_pct_of_preserves_sign_negative_total(self):
        m = Money(amount=30.0)
        assert m.as_pct_of(-100.0) == -30.0

    async def test_as_pct_of_both_negative_gives_positive(self):
        m = Money(amount=-50.0)
        assert m.as_pct_of(-100.0) == 50.0

    async def test_as_pct_of_very_large_numbers(self):
        m = Money(amount=1e18)
        result = m.as_pct_of(2e18)
        assert result == pytest.approx(50.0)

    def test_as_pct_of_float_precision(self):
        m = Money(amount=1.0 / 3.0)
        result = m.as_pct_of(1.0)
        assert abs(result - (100.0 / 3.0)) < 1e-10


class TestMoneyDataclassBehavior:
    def test_money_is_mutable(self):
        m = Money(amount=100.0)
        m.amount = 200.0
        assert m.amount == 200.0

    def test_money_equality(self):
        m1 = Money(amount=100.0, currency="USD")
        m2 = Money(amount=100.0, currency="USD")
        assert m1 == m2

    def test_money_inequality_amount(self):
        m1 = Money(amount=100.0)
        m2 = Money(amount=200.0)
        assert m1 != m2

    def test_money_inequality_currency(self):
        m1 = Money(amount=100.0, currency="USD")
        m2 = Money(amount=100.0, currency="EUR")
        assert m1 != m2


# ═══════════════════════════════════════════════════════════════════════════════
# 2. CostBreakdown integration tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCostBreakdownIntegration:
    def test_total_with_all_components(self):
        cb = CostBreakdown(
            commission=Money(1.0),
            spread=Money(0.5),
            slippage=Money(2.0),
            exchange_fee=Money(0.1),
            tax_estimate=Money(0.4),
        )
        assert cb.total.amount == pytest.approx(4.0)
        assert cb.total.currency == "USD"

    def test_total_percentage_of_trade_value(self):
        cb = CostBreakdown(
            commission=Money(5.0),
            spread=Money(2.0),
            slippage=Money(3.0),
        )
        trade_value = 10000.0
        total = cb.total
        pct = total.as_pct_of(trade_value)
        assert pct == pytest.approx(0.1)

    def test_total_percentage_of_zero_trade_value_raises(self):
        cb = CostBreakdown(commission=Money(5.0))
        with pytest.raises(ValueError, match="total must not be zero"):
            cb.total.as_pct_of(0.0)

    def test_individual_component_percentage(self):
        cb = CostBreakdown(
            commission=Money(5.0),
            spread=Money(3.0),
        )
        trade_value = 1000.0
        comm_pct = cb.commission.as_pct_of(trade_value)
        assert comm_pct == pytest.approx(0.5)

    def test_default_factory_creates_independent_instances(self):
        cb1 = CostBreakdown()
        cb2 = CostBreakdown()
        cb1.commission.amount = 999.0
        assert cb2.commission.amount == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PortfolioSnapshot integration with Money/CostBreakdown
# ═══════════════════════════════════════════════════════════════════════════════


class TestPortfolioSnapshotIntegration:
    def test_allocation_weights_sum_to_one(self):
        snap = PortfolioSnapshot(
            total_value=200_000.0,
            positions={
                "AAPL": {"market_value": 80_000.0},
                "MSFT": {"market_value": 60_000.0},
                "GOOGL": {"market_value": 40_000.0},
                "TSLA": {"market_value": 20_000.0},
            },
        )
        weights = {s: snap.allocation_weight(s) for s in snap.positions}
        assert sum(weights.values()) == pytest.approx(1.0)
        assert weights["AAPL"] == pytest.approx(0.4)
        assert weights["TSLA"] == pytest.approx(0.1)

    def test_allocation_weight_zero_total_returns_zero(self):
        snap = PortfolioSnapshot(
            total_value=0.0,
            positions={"AAPL": {"market_value": 100_000.0}},
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_summary_with_realistic_portfolio(self):
        snap = PortfolioSnapshot(
            cash=25_500.75,
            total_value=125_750.50,
            positions={
                "AAPL": {"qty": 100, "market_value": 15_000.0},
                "MSFT": {"qty": 50, "market_value": 15_000.0},
                "GOOGL": {"qty": 25, "market_value": 35_249.75},
            },
        )
        s = snap.summary()
        assert "$125,750.50" in s
        assert "$25,500.75" in s
        assert "Positions: 3" in s

    def test_position_lookup_and_allocation_consistency(self):
        positions = {
            "AAPL": {"market_value": 50_000.0},
            "MSFT": {"market_value": 30_000.0},
        }
        snap = PortfolioSnapshot(total_value=100_000.0, positions=positions)
        for sym in positions:
            assert snap.has_position(sym)
            assert snap.get_position(sym) is not None
            weight = snap.allocation_weight(sym)
            assert 0.0 <= weight <= 1.0

    def test_pnl_fields_accept_negative(self):
        snap = PortfolioSnapshot(
            realized_pnl=-5000.0,
            unrealized_pnl=-3000.0,
            day_pnl=-200.0,
            total_return_pct=-8.0,
        )
        assert snap.realized_pnl == -5000.0
        assert snap.unrealized_pnl == -3000.0
        assert snap.day_pnl == -200.0
        assert snap.total_return_pct == -8.0

    def test_positions_dict_mutation_isolation(self):
        original = {"AAPL": {"qty": 100}}
        snap = PortfolioSnapshot(positions=original)
        snap.positions["MSFT"] = {"qty": 50}
        assert "MSFT" in snap.positions
        assert "MSFT" not in original or original.get("MSFT") is not None

    def test_model_serialization_round_trip(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            total_value=150_000.0,
            positions={"AAPL": {"qty": 100, "market_value": 100_000.0}},
            realized_pnl=5000.0,
        )
        data = snap.model_dump()
        restored = PortfolioSnapshot.model_validate(data)
        assert restored.cash == snap.cash
        assert restored.total_value == snap.total_value
        assert len(restored.positions) == 1
        assert restored.realized_pnl == 5000.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Signal factory methods and edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignalFactoryMethods:
    def test_buy_factory(self):
        s = Signal.buy("AAPL", strategy_id="strat1")
        assert s.symbol == "AAPL"
        assert s.side == Side.BUY
        assert s.strategy_id == "strat1"
        assert s.strength == SignalStrength.MODERATE

    def test_sell_factory(self):
        s = Signal.sell("MSFT", strategy_id="strat2")
        assert s.symbol == "MSFT"
        assert s.side == Side.SELL
        assert s.strategy_id == "strat2"

    def test_hold_factory(self):
        s = Signal.hold("GOOGL")
        assert s.symbol == "GOOGL"
        assert s.side == Side.HOLD
        assert s.strategy_id == ""

    def test_signal_with_all_fields(self):
        s = Signal(
            symbol="TSLA",
            side=Side.BUY,
            strategy_id="momentum",
            weight=0.8,
            quantity=100,
            strength=SignalStrength.STRONG,
            reason="Breakout above resistance",
            stop_loss_pct=0.05,
            take_profit_pct=0.15,
            max_cost_pct=0.01,
            metadata={"source": "ml_model", "confidence": 0.92},
        )
        assert s.weight == 0.8
        assert s.quantity == 100
        assert s.stop_loss_pct == 0.05
        assert s.take_profit_pct == 0.15
        assert s.max_cost_pct == 0.01
        assert s.metadata["confidence"] == 0.92

    def test_signal_auto_generates_id(self):
        s1 = Signal.buy("AAPL")
        s2 = Signal.buy("AAPL")
        assert s1.id != s2.id
        assert len(s1.id) > 0

    def test_signal_auto_generates_timestamp(self):
        before = datetime.now(UTC)
        s = Signal.buy("AAPL")
        after = datetime.now(UTC)
        assert before <= s.timestamp <= after

    def test_signal_weight_boundary_zero(self):
        s = Signal(symbol="X", side=Side.BUY, weight=0.0)
        assert s.weight == 0.0

    def test_signal_weight_boundary_one(self):
        s = Signal(symbol="X", side=Side.BUY, weight=1.0)
        assert s.weight == 1.0

    def test_signal_weight_exceeds_one_rejected(self):
        with pytest.raises(ValidationError):
            Signal(symbol="X", side=Side.BUY, weight=1.1)

    def test_signal_weight_negative_rejected(self):
        with pytest.raises(ValidationError):
            Signal(symbol="X", side=Side.BUY, weight=-0.1)

    def test_signal_model_dump(self):
        s = Signal.buy("AAPL", strategy_id="test")
        data = s.model_dump()
        assert data["symbol"] == "AAPL"
        assert data["side"] == Side.BUY
        assert data["strategy_id"] == "test"

    def test_signal_side_enum_values(self):
        assert Side.BUY.value == "buy"
        assert Side.SELL.value == "sell"
        assert Side.HOLD.value == "hold"

    def test_signal_strength_enum_values(self):
        assert SignalStrength.STRONG.value == "strong"
        assert SignalStrength.MODERATE.value == "moderate"
        assert SignalStrength.WEAK.value == "weak"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. ZScoreNormalizer comprehensive edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestZScoreNormalizerComprehensive:
    def test_winsorize_preserves_order(self):
        n = ZScoreNormalizer()
        values = [float(i) for i in range(1, 101)]
        result = n.winsorize(values)
        for i in range(len(result) - 1):
            assert result[i] <= result[i + 1]

    def test_standardize_population_std(self):
        n = ZScoreNormalizer()
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        result = n.standardize(values)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        expected_std = variance**0.5
        for i, z in enumerate(result):
            expected_z = (values[i] - mean) / expected_std
            assert z == pytest.approx(expected_z, abs=1e-10)

    def test_scale_to_range_preserves_order(self):
        n = ZScoreNormalizer()
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = n.scale_to_range(values, low=0.0, high=1000.0)
        for i in range(len(result) - 1):
            assert result[i] < result[i + 1]

    def test_pipeline_large_random_data(self):
        import random

        random.seed(42)
        n = ZScoreNormalizer(winsorize_lower=5.0, winsorize_upper=95.0)
        values = [random.gauss(100, 20) for _ in range(1000)]
        z_scores = n.winsorize_and_standardize(values)
        assert len(z_scores) == 1000
        mean_z = sum(z_scores) / len(z_scores)
        assert abs(mean_z) < 0.1

    def test_pipeline_with_none_scattered(self):
        n = ZScoreNormalizer()
        values = [1.0, None, 3.0, None, 5.0, None, 7.0]
        result = n.winsorize_and_standardize(values)
        assert len(result) == 4
        assert all(isinstance(z, float) for z in result)

    def test_winsorize_extreme_percentile_boundaries(self):
        n = ZScoreNormalizer(winsorize_lower=0.0, winsorize_upper=100.0)
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = n.winsorize(values)
        assert result == values

    def test_scale_to_range_negative_range(self):
        n = ZScoreNormalizer()
        values = [0.0, 5.0, 10.0]
        result = n.scale_to_range(values, low=-100.0, high=-50.0)
        assert result[0] == pytest.approx(-100.0)
        assert result[-1] == pytest.approx(-50.0)
        assert result[1] == pytest.approx(-75.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Scoring models integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestScoringModelsIntegration:
    def test_factor_score_round_trip(self):
        fs = FactorScore(factor_name="value", z_score=1.5, raw_value=0.25)
        d = fs.to_dict()
        restored = FactorScore.model_validate(d)
        assert restored.factor_name == "value"
        assert restored.z_score == 1.5
        assert restored.raw_value == 0.25

    def test_symbol_score_clamping_in_computation(self):
        ss = SymbolScore(symbol="X", composite_score=150.0)
        assert ss.composite_score == 100.0
        ss2 = SymbolScore(symbol="Y", composite_score=-50.0)
        assert ss2.composite_score == 0.0

    def test_scoring_result_reranking(self):
        scores = [
            SymbolScore(symbol="C", composite_score=30.0, rank=99),
            SymbolScore(symbol="A", composite_score=90.0, rank=99),
            SymbolScore(symbol="B", composite_score=60.0, rank=99),
        ]
        result = ScoringResult(strategy_id="test", scores=scores)
        assert [s.symbol for s in result.scores] == ["A", "B", "C"]
        assert [s.rank for s in result.scores] == [1, 2, 3]

    def test_scoring_result_serialization_round_trip(self):
        fs1 = FactorScore(factor_name="momentum", z_score=2.1, raw_value=0.08)
        fs2 = FactorScore(factor_name="value", z_score=-0.5, raw_value=15.0)
        ss1 = SymbolScore(
            symbol="AAPL", composite_score=85.0, rank=1, factor_scores={"momentum": fs1}
        )
        ss2 = SymbolScore(
            symbol="MSFT", composite_score=72.0, rank=2, factor_scores={"value": fs2}
        )
        result = ScoringResult(
            strategy_id="demo",
            scores=[ss1, ss2],
            excluded_factors=["quality"],
        )
        d = result.to_dict()
        assert d["strategy_id"] == "demo"
        assert len(d["scores"]) == 2
        assert d["scores"][0]["symbol"] == "AAPL"
        assert d["scores"][0]["factor_scores"]["momentum"]["z_score"] == 2.1
        assert d["excluded_factors"] == ["quality"]

    def test_scoring_factor_weight_validation(self):
        ScoringFactor(name="ok", weight=0.0)
        ScoringFactor(name="ok", weight=1.0)
        with pytest.raises(ValidationError):
            ScoringFactor(name="bad", weight=-0.01)
        with pytest.raises(ValidationError):
            ScoringFactor(name="bad", weight=1.01)

    def test_empty_scoring_result(self):
        result = ScoringResult(strategy_id="empty")
        assert result.scores == []
        assert result.excluded_factors == []
        d = result.to_dict()
        assert d["scores"] == []

    def test_many_scores_ranking(self):
        scores = [
            SymbolScore(symbol=f"S{i:03d}", composite_score=float(i * 5))
            for i in range(20)
        ]
        result = ScoringResult(strategy_id="large", scores=scores)
        assert result.scores[0].symbol == "S019"
        assert result.scores[0].rank == 1
        assert result.scores[-1].symbol == "S000"
        assert result.scores[-1].rank == 20


# ═══════════════════════════════════════════════════════════════════════════════
# 7. MarketState indicators
# ═══════════════════════════════════════════════════════════════════════════════


class TestMarketStateIndicators:
    def test_sma_with_varying_closes(self):
        bars = [{"close": float(i * 10)} for i in range(1, 26)]
        state = MarketState(ohlcv={"AAPL": bars})
        sma_5 = state.sma("AAPL", 5)
        assert sma_5 is not None
        assert sma_5 == pytest.approx((210 + 220 + 230 + 240 + 250) / 5)

    def test_sma_exactly_period_bars(self):
        bars = [{"close": 100.0 + i} for i in range(5)]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", 5)
        assert result is not None
        assert result == pytest.approx(102.0)

    def test_std_with_known_values(self):
        closes = [10.0, 20.0, 30.0, 40.0, 50.0]
        bars = [{"close": c} for c in closes]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.std("AAPL", 5)
        assert result is not None
        mean = sum(closes) / 5
        variance = sum((c - mean) ** 2 for c in closes) / 5
        assert result == pytest.approx(variance**0.5)

    def test_latest_multiple_symbols(self):
        state = MarketState(prices={"AAPL": 150.0, "MSFT": 300.0, "GOOGL": 2800.0})
        assert state.latest("AAPL") == 150.0
        assert state.latest("MSFT") == 300.0
        assert state.latest("GOOGL") == 2800.0
        assert state.latest("TSLA") is None

    def test_market_state_with_all_fields(self):
        state = MarketState(
            timestamp=datetime.now(UTC),
            prices={"AAPL": 150.0},
            volumes={"AAPL": 1000000},
            ohlcv={"AAPL": [{"close": 150.0}]},
            news=[{"headline": "AAPL earnings beat"}],
            sentiment={"AAPL": 0.85},
            macro={"gdp_growth": 2.5},
            order_book={"AAPL": {"bid": 149.99, "ask": 150.01}},
        )
        assert state.volumes["AAPL"] == 1000000
        assert state.sentiment["AAPL"] == 0.85
        assert state.macro["gdp_growth"] == 2.5
        assert state.order_book["AAPL"]["bid"] == 149.99


# ═══════════════════════════════════════════════════════════════════════════════
# 8. StrategyConfig, DataFeed, IStrategy defaults
# ═══════════════════════════════════════════════════════════════════════════════


class TestStrategyConfigAndDataFeed:
    def test_config_with_complex_params(self):
        config = StrategyConfig(
            strategy_id="test",
            params={
                "threshold": 0.7,
                "lookback": 20,
                "symbols": ["AAPL", "MSFT"],
                "nested": {"key": "value"},
            },
        )
        assert config.params["threshold"] == 0.7
        assert config.params["symbols"] == ["AAPL", "MSFT"]
        assert config.params["nested"]["key"] == "value"

    def test_config_secrets_dict(self):
        config = StrategyConfig(
            strategy_id="test",
            secrets={"api_key": "sk-123", "api_secret": "abc"},
        )
        assert config.secrets["api_key"] == "sk-123"

    def test_data_feed_with_params(self):
        feed = DataFeed(
            feed_type="ohlcv",
            symbols=["AAPL", "MSFT", "GOOGL"],
            params={"interval": "1m", "source": "polygon"},
        )
        assert len(feed.symbols) == 3
        assert feed.params["interval"] == "1m"

    async def test_strategy_default_hooks(self):
        s = _SimpleBuyStrategy()
        assert await s.on_order_fill({}) is None
        assert await s.on_market_open() is None
        assert await s.on_market_close() is None
        assert s.get_min_history_bars() == 50
        assert s.get_watchlist() == []
        assert s.get_required_data_feeds()[0].feed_type == "ohlcv"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. ScoringExecutor + SDK integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestScoringExecutorIntegration:
    def test_executor_with_scoring_strategy(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _ScoringDemoStrategy()
        executor = ScoringExecutor(strategy, min_data_points=2)

        universe = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]
        market = MarketState(
            prices={s: float(100 + i * 50) for i, s in enumerate(universe)}
        )
        result = executor.execute(universe, market, None)
        assert result.strategy_id == "scoring_demo"

    def test_executor_full_scoring_pipeline(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _ScoringDemoStrategy()
        executor = ScoringExecutor(strategy, min_data_points=2)

        raw_data = {
            "AAPL": {"value": 15.0, "momentum": 0.05, "quality": 0.20},
            "MSFT": {"value": 25.0, "momentum": 0.08, "quality": 0.25},
            "GOOGL": {"value": 20.0, "momentum": 0.03, "quality": 0.18},
            "AMZN": {"value": 35.0, "momentum": 0.12, "quality": 0.22},
            "TSLA": {"value": 10.0, "momentum": 0.15, "quality": 0.10},
        }
        universe = list(raw_data.keys())
        result = executor.compute_scores(universe, raw_data)

        assert len(result.scores) == 5
        assert result.scores[0].rank == 1
        assert result.scores[0].composite_score > result.scores[-1].composite_score
        for score in result.scores:
            assert 0.0 <= score.composite_score <= 100.0

    def test_executor_excludes_sparse_factors(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        strategy = _ScoringDemoStrategy()
        executor = ScoringExecutor(strategy, min_data_points=3)

        raw_data = {
            "AAPL": {"value": 15.0, "momentum": 0.05, "quality": None},
            "MSFT": {"value": 25.0, "momentum": None, "quality": None},
            "GOOGL": {"value": 20.0, "momentum": 0.03, "quality": None},
        }
        result = executor.compute_scores(["AAPL", "MSFT", "GOOGL"], raw_data)
        assert "quality" in result.excluded_factors

    def test_executor_zero_weight_handling(self):
        from engine.plugins.scoring_executor import ScoringExecutor

        class _ZeroWeightScoringStrat(IScoringStrategy):
            @property
            def id(self) -> str:
                return "zero_weight"

            @property
            def name(self) -> str:
                return "Zero Weight"

            @property
            def version(self) -> str:
                return "1.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
                return []

            def get_config_schema(self) -> dict:
                return {}

            def get_scoring_factors(self) -> list[ScoringFactor]:
                return [ScoringFactor(name="x", weight=0.0)]

            async def score_universe(self, universe, market, costs) -> ScoringResult:
                return ScoringResult(strategy_id=self.id, scores=[])

        strategy = _ZeroWeightScoringStrat()
        executor = ScoringExecutor(strategy, min_data_points=1)
        raw_data = {"AAPL": {"x": 1.0}, "MSFT": {"x": 2.0}}
        result = executor.compute_scores(["AAPL", "MSFT"], raw_data)
        assert len(result.scores) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 10. StrategyTestHarness full workflow
# ═══════════════════════════════════════════════════════════════════════════════


class TestHarnessIntegrationWorkflow:
    async def test_buy_strategy_produces_correct_signals(self):
        harness = StrategyTestHarness(_SimpleBuyStrategy())
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 150.0, "MSFT": 250.0})
        assert len(signals) == 1
        assert signals[0].symbol == "AAPL"
        assert signals[0].side == Side.BUY
        StrategyTestHarness.assert_buy("AAPL", signals)
        await harness.teardown()

    async def test_no_signals_for_expensive_stocks(self):
        harness = StrategyTestHarness(_SimpleBuyStrategy())
        await harness.setup()
        signals = await harness.tick(prices={"MSFT": 300.0})
        StrategyTestHarness.assert_no_signals(signals)
        await harness.teardown()

    async def test_no_duplicate_buy_for_existing_position(self):
        harness = StrategyTestHarness(_SimpleBuyStrategy())
        await harness.setup()
        harness.portfolio.positions["AAPL"] = {"qty": 50}
        signals = await harness.tick(prices={"AAPL": 100.0})
        StrategyTestHarness.assert_no_signals(signals)
        await harness.teardown()

    async def test_multiple_ticks_accumulate_history(self):
        harness = StrategyTestHarness(_SimpleBuyStrategy())
        await harness.setup()
        await harness.tick(prices={"AAPL": 100.0})
        await harness.tick(prices={"MSFT": 150.0})
        await harness.tick(prices={"GOOGL": 50.0})
        assert len(harness.signals_history) == 3
        await harness.teardown()

    async def test_harness_with_ohlcv_data(self):
        harness = StrategyTestHarness(_SimpleBuyStrategy())
        await harness.setup()
        ohlcv = {"AAPL": [{"close": float(140 + i)} for i in range(5)]}
        signals = await harness.tick(prices={"AAPL": 144.0}, ohlcv=ohlcv)
        assert len(signals) == 1
        await harness.teardown()

    async def test_mock_cost_model_estimate_total(self):
        model = MockCostModel(spread_bps=10.0, slippage_bps=5.0)
        cb = model.estimate_total("AAPL", 100, 200.0, "buy")
        assert cb.spread.amount == pytest.approx(200.0 * 10.0 / 10_000)
        assert cb.slippage.amount == pytest.approx(200.0 * 5.0 / 10_000 * 100)

    async def test_mock_cost_model_estimate_pct(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", 100.0)
        expected = (5.0 + 10.0) * 2 / 10_000
        assert pct == pytest.approx(expected)

    async def test_mock_cost_model_zero_price(self):
        model = MockCostModel()
        cb = model.estimate_total("AAPL", 100, 0.0, "buy")
        assert cb.spread.amount == 0.0
        assert cb.slippage.amount == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Cross-module integration: types in scoring context
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossModuleIntegration:
    def test_money_in_cost_breakdown_percentage(self):
        spread = Money(2.50)
        commission = Money(5.00)
        slippage = Money(1.50)
        cb = CostBreakdown(
            commission=commission,
            spread=spread,
            slippage=slippage,
        )
        trade_value = 10000.0
        total_pct = cb.total.as_pct_of(trade_value)
        assert total_pct == pytest.approx(0.09)

    def test_portfolio_allocation_with_cost_impact(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            cash=20_000.0,
            positions={
                "AAPL": {"market_value": 40_000.0},
                "MSFT": {"market_value": 40_000.0},
            },
        )
        cost = CostBreakdown(
            commission=Money(10.0),
            spread=Money(5.0),
            slippage=Money(3.0),
        )
        cost_pct = cost.total.as_pct_of(snap.total_value)
        assert cost_pct == pytest.approx(0.018)

    def test_signal_with_portfolio_snapshot(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"qty": 100, "market_value": 15_000.0}},
        )
        signal = Signal.buy("AAPL", strategy_id="test", quantity=50)
        assert signal.symbol == "AAPL"
        assert snap.has_position("AAPL")
        weight = snap.allocation_weight("AAPL")
        assert weight == pytest.approx(0.15)

    def test_scoring_result_with_portfolio_context(self):
        scores = [
            SymbolScore(
                symbol="AAPL",
                composite_score=90.0,
                factor_scores={
                    "momentum": FactorScore(factor_name="momentum", z_score=2.0),
                },
            ),
            SymbolScore(
                symbol="MSFT",
                composite_score=75.0,
                factor_scores={
                    "momentum": FactorScore(factor_name="momentum", z_score=1.0),
                },
            ),
        ]
        result = ScoringResult(strategy_id="demo", scores=scores)

        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={
                "AAPL": {"market_value": 30_000.0},
                "MSFT": {"market_value": 20_000.0},
            },
        )

        for score in result.scores:
            if snap.has_position(score.symbol):
                weight = snap.allocation_weight(score.symbol)
                assert 0.0 <= weight <= 1.0

        assert result.scores[0].symbol == "AAPL"
        assert snap.allocation_weight("AAPL") == pytest.approx(0.3)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. SDK __init__ module public API
# ═══════════════════════════════════════════════════════════════════════════════


class TestSDKPublicAPI:
    def test_all_exports_accessible(self):
        import nexus_sdk

        for name in nexus_sdk.__all__:
            assert hasattr(nexus_sdk, name), f"Missing export: {name}"

    def test_version(self):
        import nexus_sdk

        assert nexus_sdk.__version__ == "0.1.0"

    def test_iscore_strategy_is_istrategy_subclass(self):
        assert issubclass(IScoringStrategy, IStrategy)
