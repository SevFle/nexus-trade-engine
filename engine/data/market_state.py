"""MarketState construction pipeline.

Provides MarketState (immutable snapshot exposed to strategies) and
MarketStateBuilder (validates raw data, computes indicators, enforces
look-ahead bias guards for backtesting).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import structlog

from engine.data.quality import DataQualityReport, DataValidator

logger = structlog.get_logger()


class ValidationError(Exception):
    """Raised when input data fails validation checks."""


@dataclass(frozen=True)
class MarketState:
    """Immutable market snapshot exposed to trading strategies."""

    timestamp: datetime
    prices: dict[str, float] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)
    ohlcv: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def latest(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    def sma(self, symbol: str, period: int = 20) -> float | None:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < period:
            return None
        closes = [b["close"] for b in bars[-period:]]
        return sum(closes) / period

    def std(self, symbol: str, period: int = 20) -> float | None:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < period:
            return None
        closes = [b["close"] for b in bars[-period:]]
        mean = sum(closes) / period
        variance = sum((c - mean) ** 2 for c in closes) / period
        return variance**0.5

    def ema(self, symbol: str, period: int = 20) -> float | None:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < period:
            return None
        closes = [b["close"] for b in bars]
        multiplier = 2.0 / (period + 1)
        value = sum(closes[:period]) / period
        for price in closes[period:]:
            value = (price - value) * multiplier + value
        return value

    def rsi(self, symbol: str, period: int = 14) -> float | None:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < period + 1:
            return None
        closes = [b["close"] for b in bars]
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(0.0, delta))
            losses.append(max(0.0, -delta))
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def macd(
        self,
        symbol: str,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> tuple[float | None, float | None, float | None] | None:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < slow + signal:
            return None
        closes = [b["close"] for b in bars]
        fast_ema = self._compute_ema_series(closes, fast)
        slow_ema = self._compute_ema_series(closes, slow)
        offset = len(fast_ema) - len(slow_ema)
        macd_line = [f - s for f, s in zip(fast_ema[offset:], slow_ema, strict=True)]
        signal_line = self._compute_ema_series(macd_line, signal)
        if not macd_line or not signal_line:
            return None
        hist = macd_line[-1] - signal_line[-1]
        return macd_line[-1], signal_line[-1], hist

    @staticmethod
    def _compute_ema_series(values: list[float], period: int) -> list[float]:
        if len(values) < period:
            return []
        multiplier = 2.0 / (period + 1)
        result: list[float] = []
        value = sum(values[:period]) / period
        result.append(value)
        for price in values[period:]:
            value = (price - value) * multiplier + value
            result.append(value)
        return result

    def get_window(self, n: int) -> MarketState:
        windowed_ohlcv: dict[str, list[dict[str, Any]]] = {}
        for symbol, bars in self.ohlcv.items():
            windowed_ohlcv[symbol] = bars[-n:] if n < len(bars) else list(bars)
        return MarketState(
            timestamp=self.timestamp,
            prices=dict(self.prices),
            volumes=dict(self.volumes),
            ohlcv=windowed_ohlcv,
        )

    def to_sdk_state(self) -> Any:
        from nexus_sdk.strategy import (  # type: ignore[import-unresolved]  # noqa: PLC0415
            MarketState as SDKMarketState,
        )

        return SDKMarketState(
            timestamp=self.timestamp,
            prices=dict(self.prices),
            volumes=dict(self.volumes),
            ohlcv={s: list(bars) for s, bars in self.ohlcv.items()},
        )


class MarketStateBuilder:
    """Constructs validated MarketState instances from raw OHLCV data."""

    def __init__(
        self,
        min_bars: int = 50,
        debug: bool = False,
        use_data_validator: bool = False,
    ) -> None:
        self._min_bars = min_bars
        self._debug = debug
        self._use_data_validator = use_data_validator
        self._validator = DataValidator() if use_data_validator else None
        self._last_report: DataQualityReport | None = None

    def build_for_backtest(
        self,
        data: dict[str, pd.DataFrame],
        timestamp: datetime,
        symbols: list[str],
    ) -> MarketState:
        prices: dict[str, float] = {}
        volumes: dict[str, int] = {}
        ohlcv: dict[str, list[dict[str, Any]]] = {}

        if self._use_data_validator:
            self._last_report = DataQualityReport()

        for symbol in symbols:
            df = data.get(symbol)
            if df is None:
                raise ValidationError(f"No data for symbol: {symbol}")
            df = self._slice_to_timestamp(df, timestamp)
            self._validate(df, symbol)

            if self._validator and self._last_report is not None:
                df = self._validator.validate(df, symbol, self._last_report)
                if len(df) < self._min_bars:
                    raise ValidationError(
                        f"Insufficient bars after data cleaning for {symbol}: "
                        f"{len(df)} < {self._min_bars}"
                    )

            bars = self._df_to_bars(df)
            bars = bars[-self._min_bars :] if len(bars) > self._min_bars else bars

            if self._debug:
                self._assert_no_look_ahead(bars, timestamp, symbol)

            if bars:
                prices[symbol] = float(bars[-1]["close"])
                volumes[symbol] = int(bars[-1]["volume"])
            ohlcv[symbol] = bars

        logger.info(
            "market_state.built",
            timestamp=str(timestamp),
            symbols=symbols,
            bars={s: len(b) for s, b in ohlcv.items()},
        )

        return MarketState(
            timestamp=timestamp,
            prices=prices,
            volumes=volumes,
            ohlcv=ohlcv,
        )

    async def build_for_live(
        self,
        provider: Any,
        symbols: list[str],
    ) -> MarketState:
        prices: dict[str, float] = {}
        volumes: dict[str, int] = {}
        ohlcv: dict[str, list[dict[str, Any]]] = {}

        if self._use_data_validator:
            self._last_report = DataQualityReport()

        latest_prices = await provider.get_multiple_prices(symbols)
        prices.update(latest_prices)

        now = datetime.now(tz=UTC)

        for symbol in symbols:
            df = await provider.get_ohlcv(symbol)
            if df is not None and not df.empty:
                self._validate(df, symbol)

                if self._validator and self._last_report is not None:
                    df = self._validator.validate(df, symbol, self._last_report)
                    if len(df) < self._min_bars:
                        raise ValidationError(
                            f"Insufficient bars after data cleaning for {symbol}: "
                            f"{len(df)} < {self._min_bars}"
                        )

                bars = self._df_to_bars(df)
                bars = bars[-self._min_bars :]
                if bars:
                    volumes[symbol] = int(bars[-1]["volume"])
                ohlcv[symbol] = bars

        return MarketState(
            timestamp=now,
            prices=prices,
            volumes=volumes,
            ohlcv=ohlcv,
        )

    def last_validation_report(self) -> DataQualityReport | None:
        return self._last_report

    def _slice_to_timestamp(self, df: pd.DataFrame, timestamp: datetime) -> pd.DataFrame:
        ts = pd.Timestamp(timestamp)
        idx_tz = getattr(df.index, "tz", None)
        if idx_tz is not None and ts.tz is None:
            ts = ts.tz_localize(idx_tz)
        elif idx_tz is None and ts.tz is not None:
            ts = ts.tz_localize(None)
        return df.loc[df.index <= ts]  # type: ignore[return-value]

    def _validate(self, df: pd.DataFrame, symbol: str) -> None:
        if len(df) < self._min_bars:
            raise ValidationError(f"Insufficient bars for {symbol}: {len(df)} < {self._min_bars}")
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns and df[col].isna().any():  # type: ignore[union-attr]
                raise ValidationError(
                    f"NaN detected in {symbol}.{col} at rows: {df.index[df[col].isna()].tolist()}"  # type: ignore[union-attr]
                )
        if df.index.duplicated().any():  # type: ignore[union-attr]
            raise ValidationError(
                f"Duplicate timestamps for {symbol}: {df.index[df.index.duplicated()].tolist()}"  # type: ignore[union-attr]
            )

    def _df_to_bars(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        bars: list[dict[str, Any]] = []
        for idx, row in df.iterrows():
            bars.append(
                {
                    "timestamp": idx,
                    "open": float(row["open"]),  # type: ignore[arg-type]
                    "high": float(row["high"]),  # type: ignore[arg-type]
                    "low": float(row["low"]),  # type: ignore[arg-type]
                    "close": float(row["close"]),  # type: ignore[arg-type]
                    "volume": int(row["volume"]),  # type: ignore[arg-type]
                }
            )
        return bars

    def _assert_no_look_ahead(
        self,
        bars: list[dict[str, Any]],
        timestamp: datetime,
        symbol: str,
    ) -> None:
        ts = pd.Timestamp(timestamp)
        for bar in bars:
            bar_ts = bar["timestamp"]
            if isinstance(bar_ts, pd.Timestamp):
                bar_tz = getattr(bar_ts, "tz", None)
                if bar_tz is not None and ts.tz is None:
                    bar_ts = bar_ts.tz_convert("UTC").tz_localize(None)
                elif bar_tz is None and ts.tz is not None:
                    bar_ts = bar_ts.tz_localize("UTC")
            assert bar_ts <= pd.Timestamp(timestamp), (
                f"Look-ahead bias detected for {symbol}: "
                f"bar at {bar_ts} is after cutoff {timestamp}"
            )
