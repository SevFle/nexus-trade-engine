"""
Tests for the plugin SDK — verify the strategy contract works correctly.
"""


import pytest
from core.cost_model import DefaultCostModel
from core.portfolio import Portfolio
from core.signal import Side, Signal
from plugins.sdk import IStrategy, MarketState, StrategyConfig


class DummyStrategy(IStrategy):
    """Minimal strategy for testing the SDK contract."""

    def __init__(self):
        self._initialized = False
        self._disposed = False

    @property
    def id(self): return "test-dummy"
    @property
    def name(self): return "Dummy"
    @property
    def version(self): return "0.0.1"

    async def initialize(self, config):
        self._initialized = True
        self._threshold = config.params.get("threshold", 100.0)

    async def dispose(self):
        self._disposed = True

    async def evaluate(self, portfolio, market, costs):
        signals = []
        for symbol, price in market.prices.items():
            if price < self._threshold:
                signals.append(Signal.buy(symbol, strategy_id=self.id, reason="cheap"))
        return signals

    def get_config_schema(self):
        return {"type": "object", "properties": {"threshold": {"type": "number", "default": 100.0}}}


class TestStrategyLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_and_dispose(self):
        s = DummyStrategy()
        assert not s._initialized

        config = StrategyConfig(strategy_id="test-dummy", params={"threshold": 150.0})
        await s.initialize(config)
        assert s._initialized
        assert s._threshold == 150.0

        await s.dispose()
        assert s._disposed

    @pytest.mark.asyncio
    async def test_evaluate_returns_signals(self):
        s = DummyStrategy()
        await s.initialize(StrategyConfig(strategy_id="test-dummy", params={"threshold": 200.0}))

        portfolio = Portfolio(initial_cash=100_000).snapshot()
        market = MarketState(prices={"AAPL": 150.0, "TSLA": 250.0})
        costs = DefaultCostModel()

        signals = await s.evaluate(portfolio, market, costs)
        assert len(signals) == 1  # Only AAPL is below 200
        assert signals[0].symbol == "AAPL"
        assert signals[0].side == Side.BUY

    @pytest.mark.asyncio
    async def test_evaluate_no_signals_when_nothing_matches(self):
        s = DummyStrategy()
        await s.initialize(StrategyConfig(strategy_id="test-dummy", params={"threshold": 10.0}))

        portfolio = Portfolio(initial_cash=100_000).snapshot()
        market = MarketState(prices={"AAPL": 150.0})
        costs = DefaultCostModel()

        signals = await s.evaluate(portfolio, market, costs)
        assert len(signals) == 0


class TestMarketState:
    def test_latest_price(self):
        m = MarketState(prices={"AAPL": 150.0})
        assert m.latest("AAPL") == 150.0
        assert m.latest("NOPE") is None

    def test_sma_calculation(self):
        bars = [{"close": float(i)} for i in range(1, 21)]
        m = MarketState(ohlcv={"AAPL": bars})
        sma = m.sma("AAPL", period=20)
        expected = sum(range(1, 21)) / 20
        assert abs(sma - expected) < 1e-6

    def test_sma_insufficient_data(self):
        bars = [{"close": 100.0}] * 5
        m = MarketState(ohlcv={"AAPL": bars})
        assert m.sma("AAPL", period=20) is None

    def test_std_calculation(self):
        bars = [{"close": 100.0}] * 20
        m = MarketState(ohlcv={"AAPL": bars})
        std = m.std("AAPL", period=20)
        assert std == 0.0  # All same value → zero std


class TestSignalCreation:
    def test_buy_signal(self):
        s = Signal.buy("AAPL", strategy_id="test", weight=0.5)
        assert s.symbol == "AAPL"
        assert s.side == Side.BUY
        assert s.weight == 0.5

    def test_sell_signal(self):
        s = Signal.sell("AAPL", strategy_id="test")
        assert s.side == Side.SELL

    def test_signal_has_unique_id(self):
        s1 = Signal.buy("AAPL", strategy_id="test")
        s2 = Signal.buy("AAPL", strategy_id="test")
        assert s1.id != s2.id
