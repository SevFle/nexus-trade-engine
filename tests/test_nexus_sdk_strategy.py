"""Comprehensive tests for nexus_sdk.strategy module.

Covers MarketState indicators, IStrategy defaults, DataFeed, and StrategyConfig.
"""

from __future__ import annotations

import pytest

from nexus_sdk.strategy import (
    DataFeed,
    IStrategy,
    MarketState,
    StrategyConfig,
)


class _ConcreteStrategy(IStrategy):
    @property
    def id(self) -> str:
        return "test_strat"

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

    async def evaluate(self, portfolio, market: MarketState, costs) -> list:
        return []

    def get_config_schema(self) -> dict:
        return {"type": "object"}


class TestMarketStateLatest:
    def test_latest_returns_price(self):
        state = MarketState(prices={"AAPL": 150.0, "MSFT": 300.0})
        assert state.latest("AAPL") == 150.0
        assert state.latest("MSFT") == 300.0

    def test_latest_missing_symbol_returns_none(self):
        state = MarketState(prices={"AAPL": 150.0})
        assert state.latest("UNKNOWN") is None

    def test_latest_empty_prices(self):
        state = MarketState()
        assert state.latest("AAPL") is None


class TestMarketStateSma:
    def test_sma_correct_calculation(self):
        bars = [{"close": float(i)} for i in range(1, 21)]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", 5)
        assert result is not None
        expected = sum(range(16, 21)) / 5
        assert abs(result - expected) < 1e-10

    def test_sma_full_period(self):
        closes = [10.0, 20.0, 30.0, 40.0, 50.0]
        bars = [{"close": c} for c in closes]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", 5)
        assert result is not None
        assert abs(result - 30.0) < 1e-10

    def test_sma_insufficient_data_returns_none(self):
        bars = [{"close": 100.0}, {"close": 101.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        assert state.sma("AAPL", 5) is None

    def test_sma_exact_period(self):
        bars = [{"close": 10.0}, {"close": 20.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL", 2)
        assert result is not None
        assert abs(result - 15.0) < 1e-10

    def test_sma_missing_symbol(self):
        state = MarketState(ohlcv={"AAPL": [{"close": 100.0}] * 20})
        assert state.sma("MSFT", 5) is None

    def test_sma_default_period_is_20(self):
        bars = [{"close": float(i)} for i in range(1, 25)]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.sma("AAPL")
        assert result is not None
        expected = sum(range(5, 25)) / 20
        assert abs(result - expected) < 1e-10


class TestMarketStateStd:
    def test_std_correct_calculation(self):
        closes = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        bars = [{"close": c} for c in closes]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.std("AAPL", 8)
        assert result is not None
        mean = sum(closes) / 8
        variance = sum((c - mean) ** 2 for c in closes) / 8
        expected = variance**0.5
        assert abs(result - expected) < 1e-10

    def test_std_insufficient_data_returns_none(self):
        bars = [{"close": 100.0}]
        state = MarketState(ohlcv={"AAPL": bars})
        assert state.std("AAPL", 5) is None

    def test_std_uniform_values_returns_zero(self):
        bars = [{"close": 50.0}] * 5
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.std("AAPL", 5)
        assert result is not None
        assert abs(result) < 1e-10

    def test_std_missing_symbol(self):
        state = MarketState(ohlcv={"AAPL": [{"close": 100.0}] * 20})
        assert state.std("MSFT", 5) is None

    def test_std_default_period_is_20(self):
        bars = [{"close": float(i)} for i in range(1, 25)]
        state = MarketState(ohlcv={"AAPL": bars})
        result = state.std("AAPL")
        assert result is not None


class TestMarketStateGetNews:
    def test_get_news_returns_all_news(self):
        news = [{"headline": "A"}, {"headline": "B"}]
        state = MarketState(news=news)
        result = state.get_news()
        assert result == news

    def test_get_news_default_hours_ignored(self):
        news = [{"headline": "A"}]
        state = MarketState(news=news)
        assert state.get_news(hours=48) == news

    def test_get_news_empty(self):
        state = MarketState()
        assert state.get_news() == []


class TestMarketStateGetMacroIndicators:
    def test_get_macro_returns_dict(self):
        macro = {"gdp_growth": 2.5, "inflation": 3.1}
        state = MarketState(macro=macro)
        assert state.get_macro_indicators() == macro

    def test_get_macro_empty(self):
        state = MarketState()
        assert state.get_macro_indicators() == {}


class TestMarketStateDefaults:
    def test_all_defaults_are_empty(self):
        state = MarketState()
        assert state.timestamp is None
        assert state.prices == {}
        assert state.volumes == {}
        assert state.ohlcv == {}
        assert state.news == []
        assert state.sentiment == {}
        assert state.macro == {}
        assert state.order_book == {}


class TestStrategyConfig:
    def test_defaults(self):
        config = StrategyConfig(strategy_id="test")
        assert config.strategy_id == "test"
        assert config.params == {}
        assert config.secrets == {}

    def test_with_params_and_secrets(self):
        config = StrategyConfig(
            strategy_id="test",
            params={"threshold": 0.5},
            secrets={"api_key": "abc"},
        )
        assert config.params["threshold"] == 0.5
        assert config.secrets["api_key"] == "abc"


class TestDataFeed:
    def test_defaults(self):
        feed = DataFeed(feed_type="ohlcv")
        assert feed.feed_type == "ohlcv"
        assert feed.symbols == []
        assert feed.params == {}

    def test_with_symbols(self):
        feed = DataFeed(feed_type="ohlcv", symbols=["AAPL", "MSFT"])
        assert feed.symbols == ["AAPL", "MSFT"]


class TestIStrategyDefaults:
    def test_default_author(self):
        strategy = _ConcreteStrategy()
        assert strategy.author == "unknown"

    def test_default_description(self):
        strategy = _ConcreteStrategy()
        assert strategy.description == ""

    def test_custom_author(self):
        class _CustomAuthor(IStrategy):
            @property
            def id(self) -> str:
                return "custom"

            @property
            def name(self) -> str:
                return "Custom"

            @property
            def version(self) -> str:
                return "1.0"

            @property
            def author(self) -> str:
                return "custom_author"

            async def initialize(self, config) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market, costs) -> list:
                return []

            def get_config_schema(self) -> dict:
                return {}

        strategy = _CustomAuthor()
        assert strategy.author == "custom_author"

    def test_custom_description(self):
        class _CustomDesc(IStrategy):
            @property
            def id(self) -> str:
                return "desc"

            @property
            def name(self) -> str:
                return "Desc"

            @property
            def version(self) -> str:
                return "1.0"

            @property
            def description(self) -> str:
                return "A custom description"

            async def initialize(self, config) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market, costs) -> list:
                return []

            def get_config_schema(self) -> dict:
                return {}

        strategy = _CustomDesc()
        assert strategy.description == "A custom description"

    async def test_default_on_order_fill(self):
        strategy = _ConcreteStrategy()
        result = await strategy.on_order_fill({"symbol": "AAPL", "qty": 100})
        assert result is None

    async def test_default_on_market_open(self):
        strategy = _ConcreteStrategy()
        result = await strategy.on_market_open()
        assert result is None

    async def test_default_on_market_close(self):
        strategy = _ConcreteStrategy()
        result = await strategy.on_market_close()
        assert result is None

    def test_default_get_required_data_feeds(self):
        strategy = _ConcreteStrategy()
        feeds = strategy.get_required_data_feeds()
        assert len(feeds) == 1
        assert feeds[0].feed_type == "ohlcv"

    def test_default_get_min_history_bars(self):
        strategy = _ConcreteStrategy()
        assert strategy.get_min_history_bars() == 50

    def test_default_get_watchlist(self):
        strategy = _ConcreteStrategy()
        assert strategy.get_watchlist() == []

    def test_overridden_get_min_history_bars(self):
        class _MoreHistory(IStrategy):
            @property
            def id(self) -> str:
                return "more_hist"

            @property
            def name(self) -> str:
                return "More History"

            @property
            def version(self) -> str:
                return "1.0"

            async def initialize(self, config) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market, costs) -> list:
                return []

            def get_config_schema(self) -> dict:
                return {}

            def get_min_history_bars(self) -> int:
                return 200

        strategy = _MoreHistory()
        assert strategy.get_min_history_bars() == 200

    def test_overridden_get_watchlist(self):
        class _WatchlistStrategy(IStrategy):
            @property
            def id(self) -> str:
                return "watchlist"

            @property
            def name(self) -> str:
                return "Watchlist"

            @property
            def version(self) -> str:
                return "1.0"

            async def initialize(self, config) -> None:
                pass

            async def dispose(self) -> None:
                pass

            async def evaluate(self, portfolio, market, costs) -> list:
                return []

            def get_config_schema(self) -> dict:
                return {}

            def get_watchlist(self) -> list[str]:
                return ["AAPL", "MSFT", "GOOGL"]

        strategy = _WatchlistStrategy()
        assert strategy.get_watchlist() == ["AAPL", "MSFT", "GOOGL"]
