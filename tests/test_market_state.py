"""Integration tests for MarketState construction pipeline.

Uses synthetic OHLCV DataFrames — no mocks of the system under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.data.market_state import MarketState, MarketStateBuilder, ValidationError

_TOLERANCE = 1e-10


def _make_ohlcv_df(
    n_bars: int,
    start: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    base_price: float = 100.0,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = [start + timedelta(days=i) for i in range(n_bars)]
    closes = base_price + np.cumsum(rng.normal(0, 1, n_bars))
    opens = closes + rng.normal(0, 0.5, n_bars)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.3, n_bars))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.3, n_bars))
    volumes = rng.integers(100_000, 1_000_000, n_bars)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


class TestMarketStateBuilderValidation:
    def test_rejects_nan_in_close(self):
        df = _make_ohlcv_df(60)
        df.iloc[5, df.columns.get_loc("close")] = float("nan")
        with pytest.raises(ValidationError, match=r"NaN"):
            MarketStateBuilder(min_bars=10).build_for_backtest(
                {"AAPL": df},
                df.index[-1],
                ["AAPL"],
            )

    def test_rejects_nan_in_volume(self):
        df = _make_ohlcv_df(60)
        df.iloc[5, df.columns.get_loc("volume")] = float("nan")
        with pytest.raises(ValidationError, match=r"NaN"):
            MarketStateBuilder(min_bars=10).build_for_backtest(
                {"AAPL": df},
                df.index[-1],
                ["AAPL"],
            )

    def test_rejects_insufficient_bars(self):
        df = _make_ohlcv_df(5)
        with pytest.raises(ValidationError, match=r"Insufficient"):
            MarketStateBuilder(min_bars=50).build_for_backtest(
                {"AAPL": df},
                df.index[-1],
                ["AAPL"],
            )

    def test_rejects_duplicate_timestamps(self):
        df = _make_ohlcv_df(60)
        dup_idx = df.index[5]
        row = df.iloc[[5]].copy()
        row.index = [dup_idx]
        df = pd.concat([df.iloc[:6], row, df.iloc[6:]])
        with pytest.raises(ValidationError, match=r"Duplicate"):
            MarketStateBuilder(min_bars=10).build_for_backtest(
                {"AAPL": df},
                df.index[-1],
                ["AAPL"],
            )


class TestMarketStateBuilderConstruction:
    def test_builds_state_with_correct_timestamp(self):
        df = _make_ohlcv_df(60)
        ts = df.index[-1]
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df},
            ts,
            ["AAPL"],
        )
        assert state.timestamp == ts

    def test_prices_contain_latest_close(self):
        df = _make_ohlcv_df(60)
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        assert abs(state.prices["AAPL"] - df["close"].iloc[-1]) < _TOLERANCE

    def test_volumes_contain_latest_volume(self):
        df = _make_ohlcv_df(60)
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        assert state.volumes["AAPL"] == int(df["volume"].iloc[-1])

    def test_multi_symbol(self):
        df_aapl = _make_ohlcv_df(60, base_price=150.0, seed=1)
        df_msft = _make_ohlcv_df(60, base_price=300.0, seed=2)
        ts = min(df_aapl.index[-1], df_msft.index[-1])
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df_aapl, "MSFT": df_msft},
            ts,
            ["AAPL", "MSFT"],
        )
        assert "AAPL" in state.prices
        assert "MSFT" in state.prices

    def test_no_future_data_past_timestamp(self):
        df = _make_ohlcv_df(60)
        cutoff_ts = df.index[40]
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df},
            cutoff_ts,
            ["AAPL"],
        )
        for bar in state.ohlcv["AAPL"]:
            bar_ts = bar["timestamp"]
            if isinstance(bar_ts, pd.Timestamp):
                bar_ts = bar_ts.to_pydatetime()
            assert bar_ts <= cutoff_ts

    def test_ohlcv_capped_to_min_bars(self):
        df = _make_ohlcv_df(200)
        ts = df.index[-1]
        _min_bars = 50
        builder = MarketStateBuilder(min_bars=_min_bars)
        state = builder.build_for_backtest({"AAPL": df}, ts, ["AAPL"])
        assert len(state.ohlcv["AAPL"]) <= _min_bars

    def test_debug_mode_no_assertion_for_correct_data(self):
        df = _make_ohlcv_df(60)
        state = MarketStateBuilder(min_bars=10, debug=True).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        assert state.prices["AAPL"] is not None

    def test_look_ahead_bias_detected_when_bars_exceed_timestamp(self):
        df = _make_ohlcv_df(60)
        builder = MarketStateBuilder(min_bars=10, debug=True)
        bars = builder._df_to_bars(df)  # noqa: SLF001
        with pytest.raises(AssertionError, match=r"Look.ahead|future"):
            builder._assert_no_look_ahead(bars, df.index[30], "AAPL")  # noqa: SLF001


class TestMarketStateIndicators:
    @pytest.fixture
    def state(self):
        df = _make_ohlcv_df(100)
        return MarketStateBuilder(min_bars=50).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )

    def test_sma_returns_correct_value(self, state):
        closes = [b["close"] for b in state.ohlcv["AAPL"]]
        _period = 20
        expected = sum(closes[-_period:]) / _period
        result = state.sma("AAPL", _period)
        assert result is not None
        assert abs(result - expected) < _TOLERANCE

    def test_sma_insufficient_data_returns_none(self, state):
        result = state.sma("AAPL", 9999)
        assert result is None

    def test_std_returns_correct_value(self, state):
        _period = 20
        closes = [b["close"] for b in state.ohlcv["AAPL"][-_period:]]
        mean = sum(closes) / _period
        variance = sum((c - mean) ** 2 for c in closes) / _period
        expected = variance**0.5
        result = state.std("AAPL", _period)
        assert result is not None
        assert abs(result - expected) < _TOLERANCE

    def test_ema_returns_value(self, state):
        result = state.ema("AAPL", 20)
        assert result is not None
        assert isinstance(result, float)

    def test_rsi_returns_value(self, state):
        result = state.rsi("AAPL", 14)
        assert result is not None
        _rsi_max = 100
        assert 0 <= result <= _rsi_max

    def test_macd_returns_tuple(self, state):
        result = state.macd("AAPL")
        assert result is not None
        macd_line, signal_line, histogram = result
        assert macd_line is not None
        assert signal_line is not None
        assert histogram is not None

    def test_latest_returns_current_price(self, state):
        assert state.latest("AAPL") == state.prices["AAPL"]

    def test_latest_missing_symbol_returns_none(self, state):
        assert state.latest("NONEXISTENT") is None


class TestMarketStateWindowedAccess:
    def test_get_window_returns_last_n_bars(self):
        df = _make_ohlcv_df(100)
        state = MarketStateBuilder(min_bars=50).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        _window_size = 10
        window = state.get_window(_window_size)
        assert isinstance(window, MarketState)
        assert len(window.ohlcv["AAPL"]) == _window_size

    def test_get_window_preserves_indicators(self):
        df = _make_ohlcv_df(100)
        state = MarketStateBuilder(min_bars=50).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        window = state.get_window(10)
        assert window.prices["AAPL"] == state.prices["AAPL"]

    def test_get_window_larger_than_data_returns_all(self):
        df = _make_ohlcv_df(20)
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        window = state.get_window(999)
        assert len(window.ohlcv["AAPL"]) == len(state.ohlcv["AAPL"])


class TestMarketStateToSdk:
    def test_to_sdk_state_raises_import_error_without_sdk(self, monkeypatch):
        # Pytest config now puts `sdk/` on pythonpath so `import nexus_sdk`
        # succeeds at runtime. Verify the *contract* that to_sdk_state
        # propagates ModuleNotFoundError when the SDK isn't installed by
        # blocking the import in this test only.
        import builtins
        import sys

        for mod in list(sys.modules):
            if mod == "nexus_sdk" or mod.startswith("nexus_sdk."):
                monkeypatch.delitem(sys.modules, mod, raising=False)

        real_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "nexus_sdk.strategy" or name.startswith("nexus_sdk."):
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)

        df = _make_ohlcv_df(60)
        state = MarketStateBuilder(min_bars=10).build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        with pytest.raises(ModuleNotFoundError):
            state.to_sdk_state()


class TestM6BuildForLiveMinBarsCheck:
    @pytest.fixture
    def provider(self):
        class FakeProvider:
            async def get_multiple_prices(self, symbols):
                return dict.fromkeys(symbols, 100.0)

            async def get_ohlcv(self, _symbol):
                return None

        return FakeProvider()

    @pytest.mark.asyncio
    async def test_build_for_live_returns_state_for_valid_data(self, provider):
        builder = MarketStateBuilder(min_bars=10, use_data_validator=True)
        state = await builder.build_for_live(provider, ["AAPL"])
        assert "AAPL" in state.prices
