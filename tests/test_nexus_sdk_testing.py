from __future__ import annotations

import pytest

from nexus_sdk.signals import Side, Signal
from nexus_sdk.testing import MockCostModel, StrategyTestHarness
from nexus_sdk.strategy import IStrategy, MarketState, StrategyConfig


class DummyStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "dummy"

    @property
    def name(self) -> str:
        return "Dummy"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def initialize(self, config: StrategyConfig) -> None:
        self._params = config.params

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        if market.prices.get("AAPL", 0) > 150:
            return [Signal.buy("AAPL", strategy_id=self.id)]
        if market.prices.get("AAPL", 0) < 100:
            return [Signal.sell("AAPL", strategy_id=self.id)]
        return [Signal.hold("AAPL", strategy_id=self.id)]

    def get_config_schema(self) -> dict:
        return {"type": "object", "properties": {}}


class TestMockCostModel:
    def test_estimate_total_returns_cost_breakdown(self):
        model = MockCostModel()
        result = model.estimate_total("AAPL", 100, 150.0, "buy")
        assert result.spread.amount > 0
        assert result.slippage.amount > 0

    def test_estimate_total_custom_bps(self):
        model = MockCostModel(spread_bps=10.0, slippage_bps=20.0)
        result = model.estimate_total("AAPL", 100, 150.0, "buy")
        assert result.spread.amount > 0

    def test_estimate_pct(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", 150.0)
        assert pct > 0
        expected = (5.0 + 10.0) * 2 / 10_000
        assert pct == expected


class TestStrategyTestHarness:
    @pytest.fixture
    def harness(self):
        return StrategyTestHarness(DummyStrategy())

    async def test_setup(self, harness):
        await harness.setup(params={"threshold": 0.7})
        assert harness.strategy._params == {"threshold": 0.7}

    async def test_tick_with_prices(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 160.0})
        assert len(signals) == 1
        assert signals[0].side == Side.BUY
        assert signals[0].symbol == "AAPL"

    async def test_tick_sell_signal(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 90.0})
        assert len(signals) == 1
        assert signals[0].side == Side.SELL

    async def test_tick_hold_signal(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 120.0})
        assert len(signals) == 1
        assert signals[0].side == Side.HOLD

    async def test_signals_history(self, harness):
        await harness.setup()
        await harness.tick(prices={"AAPL": 160.0})
        await harness.tick(prices={"AAPL": 90.0})
        assert len(harness.signals_history) == 2

    async def test_teardown(self, harness):
        await harness.setup()
        await harness.teardown()

    async def test_assert_buy_passes(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 160.0})
        StrategyTestHarness.assert_buy("AAPL", signals)

    async def test_assert_buy_fails(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 90.0})
        with pytest.raises(AssertionError, match="Expected BUY"):
            StrategyTestHarness.assert_buy("AAPL", signals)

    async def test_assert_sell_passes(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 90.0})
        StrategyTestHarness.assert_sell("AAPL", signals)

    async def test_assert_sell_fails(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 160.0})
        with pytest.raises(AssertionError, match="Expected SELL"):
            StrategyTestHarness.assert_sell("AAPL", signals)

    async def test_assert_no_signals_with_hold(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 120.0})
        StrategyTestHarness.assert_no_signals(signals)

    async def test_assert_no_signals_fails_with_buy(self, harness):
        await harness.setup()
        signals = await harness.tick(prices={"AAPL": 160.0})
        with pytest.raises(AssertionError, match="Expected no trade"):
            StrategyTestHarness.assert_no_signals(signals)

    async def test_custom_initial_cash(self):
        harness = StrategyTestHarness(DummyStrategy(), initial_cash=50_000.0)
        assert harness.portfolio.cash == 50_000.0
