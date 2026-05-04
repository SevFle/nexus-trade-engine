from __future__ import annotations

import pytest

from nexus_sdk.signals import Signal, Side, SignalStrength
from nexus_sdk.strategy import (
    DataFeed,
    IStrategy,
    MarketState,
    StrategyConfig,
)
from nexus_sdk.testing import MockCostModel, StrategyTestHarness
from nexus_sdk.types import CostBreakdown, Money, PortfolioSnapshot


class _MinimalStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "test-strat"

    @property
    def name(self) -> str:
        return "Test Strategy"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        signals = []
        for symbol, price in market.prices.items():
            if price > 100:
                signals.append(Signal.buy(symbol=symbol, strategy_id=self.id))
        return signals

    def get_config_schema(self) -> dict:
        return {}


class TestMarketStateMethods:
    def test_latest_returns_price(self):
        ms = MarketState(prices={"AAPL": 150.0})
        assert ms.latest("AAPL") == 150.0

    def test_latest_returns_none_for_missing(self):
        ms = MarketState(prices={})
        assert ms.latest("AAPL") is None

    def test_sma_returns_average(self):
        bars = [{"close": float(i)} for i in range(1, 6)]
        ms = MarketState(ohlcv={"AAPL": bars})
        result = ms.sma("AAPL", period=5)
        assert result == pytest.approx(3.0)

    def test_sma_returns_none_insufficient_data(self):
        bars = [{"close": 1.0}, {"close": 2.0}]
        ms = MarketState(ohlcv={"AAPL": bars})
        assert ms.sma("AAPL", period=5) is None

    def test_sma_returns_none_missing_symbol(self):
        ms = MarketState(ohlcv={})
        assert ms.sma("AAPL", period=5) is None

    def test_std_returns_standard_deviation(self):
        bars = [{"close": 2.0}, {"close": 4.0}, {"close": 4.0}, {"close": 4.0}, {"close": 5.0}]
        ms = MarketState(ohlcv={"AAPL": bars})
        result = ms.std("AAPL", period=5)
        assert result is not None
        assert result > 0

    def test_std_returns_none_insufficient_data(self):
        bars = [{"close": 1.0}]
        ms = MarketState(ohlcv={"AAPL": bars})
        assert ms.std("AAPL", period=5) is None

    def test_std_returns_none_missing_symbol(self):
        ms = MarketState(ohlcv={})
        assert ms.std("AAPL", period=5) is None

    def test_get_news_returns_news(self):
        news = [{"title": "Test", "sentiment": 0.5}]
        ms = MarketState(news=news)
        assert ms.get_news() == news

    def test_get_macro_indicators_returns_macro(self):
        macro = {"gdp": 2.5}
        ms = MarketState(macro=macro)
        assert ms.get_macro_indicators() == macro


class TestIStrategyDefaults:
    @pytest.fixture
    def strategy(self):
        return _MinimalStrategy()

    def test_author_default(self, strategy):
        assert strategy.author == "unknown"

    def test_description_default(self, strategy):
        assert strategy.description == ""

    @pytest.mark.asyncio
    async def test_on_order_fill_noop(self, strategy):
        await strategy.on_order_fill({"symbol": "AAPL", "qty": 100})

    @pytest.mark.asyncio
    async def test_on_market_open_noop(self, strategy):
        await strategy.on_market_open()

    @pytest.mark.asyncio
    async def test_on_market_close_noop(self, strategy):
        await strategy.on_market_close()

    def test_get_required_data_feeds(self, strategy):
        feeds = strategy.get_required_data_feeds()
        assert len(feeds) == 1
        assert isinstance(feeds[0], DataFeed)
        assert feeds[0].feed_type == "ohlcv"

    def test_get_min_history_bars(self, strategy):
        assert strategy.get_min_history_bars() == 50

    def test_get_watchlist(self, strategy):
        assert strategy.get_watchlist() == []


class TestNexusSdkSignalConstructors:
    def test_buy_constructor(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.side == Side.BUY
        assert sig.symbol == "AAPL"
        assert sig.strategy_id == "test"

    def test_sell_constructor(self):
        sig = Signal.sell(symbol="MSFT", strategy_id="test")
        assert sig.side == Side.SELL
        assert sig.symbol == "MSFT"

    def test_hold_constructor(self):
        sig = Signal.hold(symbol="GOOGL", strategy_id="test")
        assert sig.side == Side.HOLD
        assert sig.symbol == "GOOGL"

    def test_buy_with_extra_kwargs(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="t", weight=0.5, strength=SignalStrength.STRONG)
        assert sig.weight == 0.5
        assert sig.strength == SignalStrength.STRONG


class TestMoneyType:
    def test_as_pct_of(self):
        m = Money(amount=25.0)
        assert m.as_pct_of(100.0) == pytest.approx(25.0)

    def test_as_pct_of_zero_total(self):
        m = Money(amount=25.0)
        assert m.as_pct_of(0.0) == 0.0

    def test_default_currency(self):
        m = Money(amount=10.0)
        assert m.currency == "USD"


class TestCostBreakdown:
    def test_total_sums_all_components(self):
        cb = CostBreakdown(
            commission=Money(1.0),
            spread=Money(2.0),
            slippage=Money(3.0),
            exchange_fee=Money(0.5),
            tax_estimate=Money(1.5),
        )
        total = cb.total
        assert total.amount == pytest.approx(8.0)

    def test_default_total_is_zero(self):
        cb = CostBreakdown()
        assert cb.total.amount == 0.0


class TestPortfolioSnapshot:
    def test_get_position_returns_dict(self):
        pos = {"qty": 10, "market_value": 1500.0}
        snap = PortfolioSnapshot(positions={"AAPL": pos})
        assert snap.get_position("AAPL") == pos

    def test_get_position_returns_none(self):
        snap = PortfolioSnapshot(positions={})
        assert snap.get_position("AAPL") is None

    def test_has_position_true(self):
        snap = PortfolioSnapshot(positions={"AAPL": {"qty": 10}})
        assert snap.has_position("AAPL") is True

    def test_has_position_false(self):
        snap = PortfolioSnapshot(positions={})
        assert snap.has_position("MSFT") is False

    def test_allocation_weight(self):
        snap = PortfolioSnapshot(
            total_value=10000.0,
            positions={"AAPL": {"market_value": 2500.0}},
        )
        assert snap.allocation_weight("AAPL") == pytest.approx(0.25)

    def test_allocation_weight_zero_nav(self):
        snap = PortfolioSnapshot(total_value=0.0, positions={"AAPL": {"market_value": 100.0}})
        assert snap.allocation_weight("AAPL") == 0.0

    def test_allocation_weight_missing_position(self):
        snap = PortfolioSnapshot(total_value=10000.0, positions={})
        assert snap.allocation_weight("AAPL") == 0.0

    def test_summary(self):
        snap = PortfolioSnapshot(cash=5000.0, total_value=10000.0, positions={"AAPL": {}})
        s = snap.summary()
        assert "NAV:" in s
        assert "Cash:" in s
        assert "Positions: 1" in s


class TestMockCostModel:
    def test_estimate_total_returns_costbreakdown(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        cb = model.estimate_total("AAPL", quantity=100, price=150.0, side="buy")
        assert isinstance(cb, CostBreakdown)
        assert cb.spread.amount > 0
        assert cb.slippage.amount > 0

    def test_estimate_pct(self):
        model = MockCostModel(spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", price=150.0)
        assert pct > 0

    def test_estimate_pct_custom_params(self):
        model = MockCostModel(spread_bps=10.0, slippage_bps=20.0)
        pct = model.estimate_pct("AAPL", price=100.0)
        expected = (10.0 + 20.0) * 2 / 10_000
        assert pct == pytest.approx(expected)


class TestStrategyTestHarness:
    @pytest.mark.asyncio
    async def test_setup_and_tick(self):
        strategy = _MinimalStrategy()
        harness = StrategyTestHarness(strategy, initial_cash=50000.0)
        await harness.setup(params={"threshold": 0.5})

        signals = await harness.tick(prices={"AAPL": 150.0, "MSFT": 50.0})
        assert any(s.symbol == "AAPL" for s in signals)
        assert len(harness.signals_history) == 1

        await harness.teardown()

    @pytest.mark.asyncio
    async def test_tick_with_ohlcv(self):
        strategy = _MinimalStrategy()
        harness = StrategyTestHarness(strategy)
        await harness.setup()

        ohlcv = {"AAPL": [{"close": 150.0, "open": 148.0}]}
        signals = await harness.tick(prices={"AAPL": 150.0}, ohlcv=ohlcv)
        assert len(signals) >= 1

        await harness.teardown()

    def test_assert_buy_passes(self):
        signals = [Signal.buy("AAPL", "t")]
        StrategyTestHarness.assert_buy("AAPL", signals)

    def test_assert_buy_fails(self):
        with pytest.raises(AssertionError, match="Expected BUY"):
            StrategyTestHarness.assert_buy("MSFT", [Signal.buy("AAPL", "t")])

    def test_assert_sell_passes(self):
        signals = [Signal.sell("AAPL", "t")]
        StrategyTestHarness.assert_sell("AAPL", signals)

    def test_assert_sell_fails(self):
        with pytest.raises(AssertionError, match="Expected SELL"):
            StrategyTestHarness.assert_sell("MSFT", [Signal.sell("AAPL", "t")])

    def test_assert_no_signals_passes(self):
        signals = [Signal.hold("AAPL", "t")]
        StrategyTestHarness.assert_no_signals(signals)

    def test_assert_no_signals_fails(self):
        with pytest.raises(AssertionError, match="Expected no trade"):
            StrategyTestHarness.assert_no_signals([Signal.buy("AAPL", "t")])


class TestStrategyConfig:
    def test_defaults(self):
        config = StrategyConfig(strategy_id="test")
        assert config.params == {}
        assert config.secrets == {}

    def test_custom_params(self):
        config = StrategyConfig(strategy_id="test", params={"threshold": 0.5}, secrets={"api_key": "xxx"})
        assert config.params["threshold"] == 0.5
        assert config.secrets["api_key"] == "xxx"


class TestDataFeed:
    def test_defaults(self):
        feed = DataFeed(feed_type="ohlcv")
        assert feed.symbols == []
        assert feed.params == {}

    def test_custom(self):
        feed = DataFeed(feed_type="ohlcv", symbols=["AAPL", "MSFT"], params={"interval": "1m"})
        assert len(feed.symbols) == 2
        assert feed.params["interval"] == "1m"
