"""Comprehensive tests for nexus_sdk.testing module.

Covers MockCostModel, StrategyTestHarness, and assertion helpers.
"""

from __future__ import annotations

import pytest

from nexus_sdk.signals import Side, Signal
from nexus_sdk.strategy import (
    DataFeed,
    IStrategy,
    MarketState,
    StrategyConfig,
)
from nexus_sdk.testing import MockCostModel, StrategyTestHarness
from nexus_sdk.types import CostBreakdown, Money, PortfolioSnapshot


class _BuyEverythingStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "buy_everything"

    @property
    def name(self) -> str:
        return "Buy Everything"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def author(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return "Buys every symbol passed"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        return [Signal.buy(sym, strategy_id=self.id) for sym in market.prices]

    def get_config_schema(self) -> dict:
        return {"type": "object"}


class _SellEverythingStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "sell_everything"

    @property
    def name(self) -> str:
        return "Sell Everything"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        return [Signal.sell(sym, strategy_id=self.id) for sym in market.prices]

    def get_config_schema(self) -> dict:
        return {}


class _HoldStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "hold_strategy"

    @property
    def name(self) -> str:
        return "Hold"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        return [Signal.hold(sym, strategy_id=self.id) for sym in market.prices]

    def get_config_schema(self) -> dict:
        return {}


class _ConfigCapturingStrategy(IStrategy):
    captured_config: StrategyConfig | None = None

    @property
    def id(self) -> str:
        return "config_capture"

    @property
    def name(self) -> str:
        return "Config Capture"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        _ConfigCapturingStrategy.captured_config = config

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        return []

    def get_config_schema(self) -> dict:
        return {}


class TestMockCostModel:
    def test_estimate_total_returns_cost_breakdown(self):
        model = MockCostModel()
        result = model.estimate_total("AAPL", 100, 150.0, "buy")
        assert isinstance(result, CostBreakdown)
        assert result.spread.amount > 0
        assert result.slippage.amount > 0

    def test_estimate_total_uses_spread_bps(self):
        model = MockCostModel(spread_bps=10.0)
        result = model.estimate_total("AAPL", 1, 100.0, "buy")
        expected_spread = 100.0 * (10.0 / 10_000)
        assert abs(result.spread.amount - expected_spread) < 1e-10

    def test_estimate_total_uses_slippage_bps(self):
        model = MockCostModel(slippage_bps=20.0)
        qty = 50
        result = model.estimate_total("AAPL", qty, 200.0, "buy")
        expected_slippage = 200.0 * (20.0 / 10_000) * qty
        assert abs(result.slippage.amount - expected_slippage) < 1e-10

    def test_estimate_total_commission_default_zero(self):
        model = MockCostModel()
        result = model.estimate_total("AAPL", 100, 150.0, "buy")
        assert result.commission.amount == 0.0

    def test_estimate_total_default_bps(self):
        model = MockCostModel()
        result = model.estimate_total("AAPL", 100, 100.0, "buy")
        assert result.spread.amount > 0
        assert result.slippage.amount > 0

    def test_estimate_total_zero_price(self):
        model = MockCostModel()
        result = model.estimate_total("AAPL", 100, 0.0, "buy")
        assert result.spread.amount == 0.0
        assert result.slippage.amount == 0.0

    def test_estimate_pct_returns_float(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", 100.0, "buy")
        assert isinstance(pct, float)
        assert pct > 0

    def test_estimate_pct_formula(self):
        spread_bps = 5.0
        slippage_bps = 10.0
        model = MockCostModel(spread_bps=spread_bps, slippage_bps=slippage_bps)
        pct = model.estimate_pct("AAPL", 100.0)
        expected = (spread_bps + slippage_bps) * 2 / 10_000
        assert abs(pct - expected) < 1e-10

    def test_estimate_pct_buy_side(self):
        model = MockCostModel()
        pct_buy = model.estimate_pct("AAPL", 100.0, "buy")
        assert pct_buy > 0

    def test_estimate_pct_sell_side(self):
        model = MockCostModel()
        pct_sell = model.estimate_pct("AAPL", 100.0, "sell")
        assert pct_sell > 0


class TestStrategyTestHarnessInit:
    def test_default_initial_cash(self):
        strategy = _HoldStrategy()
        harness = StrategyTestHarness(strategy)
        assert harness.portfolio.cash == 100_000.0
        assert harness.portfolio.total_value == 100_000.0

    def test_custom_initial_cash(self):
        strategy = _HoldStrategy()
        harness = StrategyTestHarness(strategy, initial_cash=50_000.0)
        assert harness.portfolio.cash == 50_000.0
        assert harness.portfolio.total_value == 50_000.0

    def test_strategy_stored(self):
        strategy = _HoldStrategy()
        harness = StrategyTestHarness(strategy)
        assert harness.strategy is strategy

    def test_cost_model_created(self):
        strategy = _HoldStrategy()
        harness = StrategyTestHarness(strategy)
        assert isinstance(harness.cost_model, MockCostModel)

    def test_signals_history_empty(self):
        strategy = _HoldStrategy()
        harness = StrategyTestHarness(strategy)
        assert harness.signals_history == []


class TestStrategyTestHarnessSetup:
    async def test_setup_calls_initialize_with_config(self):
        _ConfigCapturingStrategy.captured_config = None
        strategy = _ConfigCapturingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup(params={"threshold": 0.7})
        config = _ConfigCapturingStrategy.captured_config
        assert config is not None
        assert config.strategy_id == "config_capture"
        assert config.params == {"threshold": 0.7}

    async def test_setup_with_secrets(self):
        _ConfigCapturingStrategy.captured_config = None
        strategy = _ConfigCapturingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup(secrets={"api_key": "secret123"})
        config = _ConfigCapturingStrategy.captured_config
        assert config.secrets == {"api_key": "secret123"}

    async def test_setup_default_empty_params(self):
        _ConfigCapturingStrategy.captured_config = None
        strategy = _ConfigCapturingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        config = _ConfigCapturingStrategy.captured_config
        assert config.params == {}
        assert config.secrets == {}


class TestStrategyTestHarnessTick:
    async def test_tick_with_prices(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 150.0, "MSFT": 300.0})
        assert len(signals) == 2
        symbols = {s.symbol for s in signals}
        assert symbols == {"AAPL", "MSFT"}

    async def test_tick_with_ohlcv(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        ohlcv = {
            "AAPL": [
                {"close": 148.0},
                {"close": 149.0},
                {"close": 150.0},
            ]
        }
        signals = await harness.tick(prices={"AAPL": 150.0}, ohlcv=ohlcv)
        assert len(signals) == 1
        assert signals[0].symbol == "AAPL"

    async def test_tick_with_news(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        news = [{"headline": "AAPL beats earnings"}]
        signals = await harness.tick(prices={"AAPL": 155.0}, news=news)
        assert len(signals) == 1

    async def test_tick_empty_market(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        signals = await harness.tick()
        assert signals == []

    async def test_tick_appends_to_history(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        await harness.tick(prices={"AAPL": 150.0})
        await harness.tick(prices={"AAPL": 151.0, "MSFT": 300.0})
        assert len(harness.signals_history) == 2
        assert len(harness.signals_history[0]) == 1
        assert len(harness.signals_history[1]) == 2

    async def test_tick_passes_portfolio_to_strategy(self):
        captured_portfolio = None

        class _PortfolioCapture(IStrategy):
            @property
            def id(self) -> str:
                return "port_capture"

            @property
            def name(self) -> str:
                return "Port Capture"

            @property
            def version(self) -> str:
                return "1.0.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market, costs):
                nonlocal captured_portfolio
                captured_portfolio = portfolio
                return []

            def get_config_schema(self) -> dict:
                return {}

        strategy = _PortfolioCapture()
        harness = StrategyTestHarness(strategy, initial_cash=75_000.0)
        await harness.setup()
        await harness.tick(prices={"AAPL": 150.0})
        assert captured_portfolio is not None
        assert captured_portfolio.cash == 75_000.0


class TestStrategyTestHarnessTeardown:
    async def test_teardown_calls_dispose(self):
        disposed = False

        class _DisposeTracker(IStrategy):
            @property
            def id(self) -> str:
                return "dispose_tracker"

            @property
            def name(self) -> str:
                return "Dispose Tracker"

            @property
            def version(self) -> str:
                return "1.0.0"

            async def initialize(self, config: StrategyConfig) -> None:
                pass

            async def dispose(self) -> None:
                nonlocal disposed
                disposed = True

            async def evaluate(self, portfolio, market, costs):
                return []

            def get_config_schema(self) -> dict:
                return {}

        strategy = _DisposeTracker()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        await harness.teardown()
        assert disposed


class TestAssertBuy:
    def test_assert_buy_succeeds_with_buy_signal(self):
        signals = [Signal.buy("AAPL"), Signal.hold("MSFT")]
        StrategyTestHarness.assert_buy("AAPL", signals)

    def test_assert_buy_raises_without_buy_signal(self):
        signals = [Signal.sell("AAPL"), Signal.hold("MSFT")]
        with pytest.raises(AssertionError, match="Expected BUY signal for MSFT"):
            StrategyTestHarness.assert_buy("MSFT", signals)

    def test_assert_buy_raises_on_empty_list(self):
        with pytest.raises(AssertionError, match="Expected BUY signal"):
            StrategyTestHarness.assert_buy("AAPL", [])

    def test_assert_buy_multiple_buy_signals(self):
        signals = [Signal.buy("AAPL"), Signal.buy("AAPL")]
        StrategyTestHarness.assert_buy("AAPL", signals)


class TestAssertSell:
    def test_assert_sell_succeeds_with_sell_signal(self):
        signals = [Signal.sell("AAPL"), Signal.hold("MSFT")]
        StrategyTestHarness.assert_sell("AAPL", signals)

    def test_assert_sell_raises_without_sell_signal(self):
        signals = [Signal.buy("AAPL")]
        with pytest.raises(AssertionError, match="Expected SELL signal for AAPL"):
            StrategyTestHarness.assert_sell("AAPL", signals)

    def test_assert_sell_raises_on_empty_list(self):
        with pytest.raises(AssertionError, match="Expected SELL signal"):
            StrategyTestHarness.assert_sell("AAPL", [])


class TestAssertNoSignals:
    def test_assert_no_signals_with_hold_only(self):
        signals = [Signal.hold("AAPL"), Signal.hold("MSFT")]
        StrategyTestHarness.assert_no_signals(signals)

    def test_assert_no_signals_with_empty_list(self):
        StrategyTestHarness.assert_no_signals([])

    def test_assert_no_signals_raises_with_buy(self):
        signals = [Signal.buy("AAPL")]
        with pytest.raises(AssertionError, match="Expected no trade signals"):
            StrategyTestHarness.assert_no_signals(signals)

    def test_assert_no_signals_raises_with_sell(self):
        signals = [Signal.sell("MSFT")]
        with pytest.raises(AssertionError, match="Expected no trade signals"):
            StrategyTestHarness.assert_no_signals(signals)


class TestHarnessFullWorkflow:
    async def test_full_buy_workflow(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup(params={"threshold": 0.5})
        signals = await harness.tick(prices={"AAPL": 150.0})
        StrategyTestHarness.assert_buy("AAPL", signals)
        await harness.teardown()
        assert len(harness.signals_history) == 1

    async def test_full_sell_workflow(self):
        strategy = _SellEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 150.0})
        StrategyTestHarness.assert_sell("AAPL", signals)
        await harness.teardown()

    async def test_full_hold_workflow(self):
        strategy = _HoldStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 150.0})
        StrategyTestHarness.assert_no_signals(signals)
        await harness.teardown()

    async def test_multiple_ticks_accumulate_history(self):
        strategy = _BuyEverythingStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()
        await harness.tick(prices={"AAPL": 150.0})
        await harness.tick(prices={"AAPL": 151.0})
        await harness.tick(prices={"AAPL": 152.0, "MSFT": 300.0})
        assert len(harness.signals_history) == 3
        assert len(harness.signals_history[0]) == 1
        assert len(harness.signals_history[1]) == 1
        assert len(harness.signals_history[2]) == 2
