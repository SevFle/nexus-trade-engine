"""Data Quality & Integrity Layer for Market Data Feeds.

Sits between MarketDataProvider and MarketState construction.
Validates, cleans, and reports on corrupt data from yfinance and other feeds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import numpy as np
import structlog

if TYPE_CHECKING:
    import pandas as pd

logger = structlog.get_logger()


@dataclass(frozen=True)
class ValidationConfig:
    max_pe_ratio: float = 500.0
    max_pb_ratio: float = 100.0
    min_price: float = 0.0
    stale_consecutive_days: int = 5
    stale_volume_threshold: int = 0
    gbp_detection_factor: float = 80.0
    negative_revenue_tolerance: float = 0.1
    return_spike_zscore: float = 8.0
    return_spike_min_window: int = 20

    @classmethod
    def from_dict(cls, data: dict) -> ValidationConfig:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True)
class CorrectionRecord:
    symbol: str
    field: str
    original_value: float | None
    corrected_value: float | None
    reason: str
    action: str


class DataQualityReport:
    def __init__(self) -> None:
        self.corrections: list[CorrectionRecord] = []
        self._symbols_seen: set[str] = set()

    def record_symbol(self, symbol: str) -> None:
        self._symbols_seen.add(symbol)

    def add_correction(
        self,
        symbol: str,
        field: str,
        original_value: float | None,
        corrected_value: float | None,
        reason: str,
        action: str,
    ) -> None:
        self.corrections.append(
            CorrectionRecord(
                symbol=symbol,
                field=field,
                original_value=original_value,
                corrected_value=corrected_value,
                reason=reason,
                action=action,
            )
        )

    @property
    def flagged_percentage(self) -> float:
        flagged = {c.symbol for c in self.corrections}
        if not self._symbols_seen:
            return 0.0
        return len(flagged) / len(self._symbols_seen) * 100.0

    def flagged_percentage_for_universe(self, universe: list[str]) -> float:
        flagged = {c.symbol for c in self.corrections}
        unique_universe = set(universe)
        if not unique_universe:
            return 0.0
        flagged_in_universe = flagged.intersection(unique_universe)
        return len(flagged_in_universe) / len(unique_universe) * 100.0

    def should_warn(self, universe: list[str], threshold_pct: float = 10.0) -> bool:
        return self.flagged_percentage_for_universe(universe) > threshold_pct

    def corrections_by_symbol(self) -> dict[str, list[CorrectionRecord]]:
        result: dict[str, list[CorrectionRecord]] = {}
        for c in self.corrections:
            result.setdefault(c.symbol, []).append(c)
        return result


class ValidationRule(ABC):
    @abstractmethod
    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **kwargs: object,
    ) -> pd.DataFrame: ...


class PriceBoundsRule(ValidationRule):
    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **_kwargs: object,
    ) -> pd.DataFrame:
        mask = df["close"] <= 0
        for idx in df.index[mask]:
            report.add_correction(
                symbol=symbol,
                field="close",
                original_value=float(df.loc[idx, "close"]),
                corrected_value=None,
                reason="negative_price",
                action="exclude",
            )
        df = cast("pd.DataFrame", df[~mask].copy())

        if self._config.min_price > 0:
            min_mask = (df["close"] > 0) & (df["close"] < self._config.min_price)
            for idx in df.index[min_mask]:
                report.add_correction(
                    symbol=symbol,
                    field="close",
                    original_value=float(df.loc[idx, "close"]),
                    corrected_value=None,
                    reason="price_below_minimum",
                    action="exclude",
                )
            df = cast("pd.DataFrame", df[~min_mask].copy())

        return df


class RatioSanityRule(ValidationRule):
    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **kwargs: object,
    ) -> pd.DataFrame:
        fundamentals = kwargs.get("fundamentals")
        if fundamentals is not None:
            fundamentals_df = cast("pd.DataFrame", fundamentals)
            fundamentals_df = self.apply_fundamentals(symbol, report, fundamentals_df)

            if "eps" in fundamentals_df.columns:
                last_eps = fundamentals_df["eps"].iloc[-1] if len(fundamentals_df) > 0 else None
                if last_eps is not None and last_eps > 0:
                    last_close = float(df["close"].iloc[-1])
                    pe = last_close / last_eps
                    if pe > self._config.max_pe_ratio:
                        report.add_correction(
                            symbol=symbol,
                            field="pe_ratio",
                            original_value=pe,
                            corrected_value=None,
                            reason="impossible_pe_ratio",
                            action="flag",
                        )

        return df

    def apply_fundamentals(
        self,
        symbol: str,
        report: DataQualityReport,
        fundamentals: pd.DataFrame,
    ) -> pd.DataFrame:
        fundamentals = fundamentals.copy()

        if "revenue" in fundamentals.columns:
            fundamentals = self._check_revenue(symbol, report, fundamentals)

        if "book_value" in fundamentals.columns and "revenue" in fundamentals.columns:
            self._check_book_value(symbol, report, fundamentals)

        return fundamentals

    def _check_revenue(
        self,
        symbol: str,
        report: DataQualityReport,
        fundamentals: pd.DataFrame,
    ) -> pd.DataFrame:
        revenue = fundamentals["revenue"].values.copy()
        neg_indices = self._find_suspect_negative_revenue_indices(revenue)

        if not neg_indices:
            return fundamentals

        all_negative = all(r < 0 for r in revenue)
        if all_negative:
            return fundamentals

        positive_revenues = [r for r in revenue if r > 0]
        mean_positive = (
            sum(positive_revenues) / len(positive_revenues) if positive_revenues else 0.0
        )
        tolerance = self._config.negative_revenue_tolerance

        for i in neg_indices:
            if (
                tolerance > 0
                and mean_positive > 0
                and abs(revenue[i]) <= tolerance * mean_positive
            ):
                continue
            neighbors = []
            if i > 0 and revenue[i - 1] > 0:
                neighbors.append(revenue[i - 1])
            if i < len(revenue) - 1 and revenue[i + 1] > 0:
                neighbors.append(revenue[i + 1])
            original_value = float(revenue[i])
            if neighbors:
                interpolated = sum(neighbors) / len(neighbors)
                fundamentals.iloc[i, fundamentals.columns.get_loc("revenue")] = interpolated
                report.add_correction(
                    symbol=symbol,
                    field="revenue",
                    original_value=original_value,
                    corrected_value=interpolated,
                    reason="negative_revenue",
                    action="interpolate",
                )
            else:
                report.add_correction(
                    symbol=symbol,
                    field="revenue",
                    original_value=original_value,
                    corrected_value=None,
                    reason="negative_revenue",
                    action="interpolate",
                )

        return fundamentals

    @staticmethod
    def _find_suspect_negative_revenue_indices(revenue: object) -> list[int]:
        rev_arr = list(cast("list[float]", revenue))
        neg_indices: list[int] = []
        for i in range(len(rev_arr)):
            if rev_arr[i] >= 0:
                continue
            has_positive_neighbor = False
            if i > 0 and rev_arr[i - 1] > 0:
                has_positive_neighbor = True
            if i < len(rev_arr) - 1 and rev_arr[i + 1] > 0:
                has_positive_neighbor = True
            if has_positive_neighbor:
                neg_indices.append(i)
        return neg_indices

    def _check_book_value(
        self,
        symbol: str,
        report: DataQualityReport,
        fundamentals: pd.DataFrame,
    ) -> None:
        book_values = fundamentals["book_value"].values
        revenues = fundamentals["revenue"].values
        all_positive_revenue = all(r > 0 for r in revenues)
        any_negative_book = any(bv < 0 for bv in book_values)
        if all_positive_revenue and any_negative_book:
            report.add_correction(
                symbol=symbol,
                field="book_value",
                original_value=float(book_values[-1]),
                corrected_value=None,
                reason="impossible_pb_ratio",
                action="flag",
            )


class GBpGBPRule(ValidationRule):
    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **kwargs: object,
    ) -> pd.DataFrame:
        sector_median_price = kwargs.get("sector_median_price")
        if sector_median_price is None:
            return df

        sector_median = float(cast("float", sector_median_price))
        if sector_median <= 0:
            return df
        median_close = float(cast("float", df["close"].median()))
        ratio = median_close / sector_median

        if ratio > self._config.gbp_detection_factor:
            factor = 100.0
            report.add_correction(
                symbol=symbol,
                field="close",
                original_value=median_close,
                corrected_value=median_close / factor,
                reason="gbp_gbp_mismatch",
                action="normalize",
            )
            df = df.copy()
            for col in ("open", "high", "low", "close"):
                df[col] = df[col] / factor
            return df

        return df


class StaleDataRule(ValidationRule):
    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **_kwargs: object,
    ) -> pd.DataFrame:
        consecutive = 0
        stale_start_idx: int | None = None
        threshold = self._config.stale_consecutive_days
        vol_threshold = self._config.stale_volume_threshold

        for i in range(len(df)):
            vol = df.iloc[i]["volume"]
            if vol <= vol_threshold:
                if consecutive == 0:
                    stale_start_idx = i
                consecutive += 1
            else:
                if consecutive >= threshold and stale_start_idx is not None:
                    self._report_stale(symbol, report, df, stale_start_idx, i - 1)
                consecutive = 0
                stale_start_idx = None

        if consecutive >= threshold and stale_start_idx is not None:
            self._report_stale(symbol, report, df, stale_start_idx, len(df) - 1)

        return df

    def _report_stale(
        self,
        symbol: str,
        report: DataQualityReport,
        _df: pd.DataFrame,
        start: int,
        end: int,
    ) -> None:
        report.add_correction(
            symbol=symbol,
            field="volume",
            original_value=0.0,
            corrected_value=None,
            reason="stale_data",
            action="flag",
        )
        logger.warning(
            "data_quality.stale_data",
            symbol=symbol,
            start_idx=start,
            end_idx=end,
            consecutive_days=end - start + 1,
        )


class OHLCConsistencyRule(ValidationRule):
    """Flag bars where OHLC fields violate basic ordering invariants.

    Detects:
    - high < max(open, close)  → ohlc_high_below_body
    - low  > min(open, close)  → ohlc_low_above_body
    - high < low               → ohlc_high_below_low
    - volume < 0               → ohlc_negative_volume

    Default action is "flag" — bars are not dropped because OHLC bands
    can be subtly off from data-vendor rounding without being unusable
    for backtests. The downstream cost / risk path can decide.
    """

    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **_kwargs: object,
    ) -> pd.DataFrame:
        if df.empty:
            return df
        body_max = df[["open", "close"]].max(axis=1)
        body_min = df[["open", "close"]].min(axis=1)
        high_below_body = df["high"] < body_max
        low_above_body = df["low"] > body_min
        high_below_low = df["high"] < df["low"]
        for idx in df.index[high_below_body]:
            report.add_correction(
                symbol=symbol,
                field="high",
                original_value=float(df.loc[idx, "high"]),
                corrected_value=None,
                reason="ohlc_high_below_body",
                action="flag",
            )
        for idx in df.index[low_above_body]:
            report.add_correction(
                symbol=symbol,
                field="low",
                original_value=float(df.loc[idx, "low"]),
                corrected_value=None,
                reason="ohlc_low_above_body",
                action="flag",
            )
        for idx in df.index[high_below_low]:
            report.add_correction(
                symbol=symbol,
                field="high",
                original_value=float(df.loc[idx, "high"]),
                corrected_value=None,
                reason="ohlc_high_below_low",
                action="flag",
            )
        if "volume" in df.columns:
            neg_vol = df["volume"] < 0
            for idx in df.index[neg_vol]:
                report.add_correction(
                    symbol=symbol,
                    field="volume",
                    original_value=float(df.loc[idx, "volume"]),
                    corrected_value=None,
                    reason="ohlc_negative_volume",
                    action="flag",
                )
        return df


class ReturnSpikeRule(ValidationRule):
    """Flag bars whose log return modified-z-score exceeds a threshold.

    Uses a MAD-based modified z-score (Iglewicz & Hoaglin) rather than
    classical mean/std because std is dragged up by the very outliers
    we want to flag — a single 5x spike in a 60-bar series can
    self-mask under classical sigma. MAD is robust to a small fraction
    of contamination.

    Skips series shorter than ``return_spike_min_window``: a small
    sample has unreliable scale estimates and produces false positives.
    """

    _MAD_FACTOR = 0.6745  # 75th percentile of standard normal — makes
    # MAD-z comparable to classical z under normality.

    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **_kwargs: object,
    ) -> pd.DataFrame:
        window = self._config.return_spike_min_window
        if len(df) < window:
            return df
        closes = df["close"].astype(float)
        if (closes <= 0).any():
            return df
        log_returns = np.log(closes / closes.shift(1)).dropna()
        if log_returns.empty:
            return df
        median = float(log_returns.median())
        abs_dev = (log_returns - median).abs()
        mad = float(abs_dev.median())
        if mad == 0.0 or not np.isfinite(mad):
            return df
        z = self._MAD_FACTOR * (log_returns - median) / mad
        threshold = self._config.return_spike_zscore
        flagged_idx = log_returns.index[z.abs() > threshold]
        for idx in flagged_idx:
            report.add_correction(
                symbol=symbol,
                field="close",
                original_value=float(df.loc[idx, "close"]),
                corrected_value=None,
                reason="return_spike",
                action="flag",
            )
        return df


class DuplicateTimestampRule(ValidationRule):
    """Drop duplicate timestamps, keeping the last (most recent) write.

    Vendor backfill jobs can re-emit a previously seen bar. Keeping the
    later write matches what a streaming consumer would have observed
    after backfill resolution.
    """

    def apply(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        **_kwargs: object,
    ) -> pd.DataFrame:
        dup_mask = df.index.duplicated(keep="last")
        if not dup_mask.any():
            return df
        for _ in df.index[dup_mask].unique():
            report.add_correction(
                symbol=symbol,
                field="timestamp",
                original_value=None,
                corrected_value=None,
                reason="duplicate_timestamp",
                action="dedup",
            )
        return cast("pd.DataFrame", df[~dup_mask].copy())


class DataValidator:
    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()
        self._rules: list[ValidationRule] = [
            DuplicateTimestampRule(),
            PriceBoundsRule(self._config),
            OHLCConsistencyRule(self._config),
            ReturnSpikeRule(self._config),
            StaleDataRule(self._config),
            RatioSanityRule(self._config),
            GBpGBPRule(self._config),
        ]

    def validate(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        fundamentals: pd.DataFrame | None = None,
        sector_median_price: float | None = None,
    ) -> pd.DataFrame:
        report.record_symbol(symbol)
        kwargs: dict = {}
        if fundamentals is not None:
            kwargs["fundamentals"] = fundamentals
        if sector_median_price is not None:
            kwargs["sector_median_price"] = sector_median_price

        pre_count = len(report.corrections)
        for rule in self._rules:
            df = rule.apply(df, symbol, report, **kwargs)

        new_corrections = len(report.corrections) - pre_count
        if new_corrections > 0:
            logger.info(
                "data_quality.corrections_applied",
                symbol=symbol,
                correction_count=new_corrections,
                reasons=list({c.reason for c in report.corrections[pre_count:]}),
            )

        return df
