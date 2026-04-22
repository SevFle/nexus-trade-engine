"""Data Quality & Integrity Layer for Market Data Feeds.

Sits between MarketDataProvider and MarketState construction.
Validates, cleans, and reports on corrupt data from yfinance and other feeds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

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
        return 0.0

    def flagged_percentage_for_universe(self, universe: list[str]) -> float:
        flagged = {c.symbol for c in self.corrections}
        if not universe:
            return 0.0
        flagged_in_universe = flagged.intersection(universe)
        return len(flagged_in_universe) / len(universe) * 100.0

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
            self.apply_fundamentals(symbol, report, fundamentals_df)

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
        revenue = fundamentals["revenue"].values
        neg_indices = self._find_suspect_negative_revenue_indices(revenue)

        if not neg_indices:
            return fundamentals

        all_negative = all(r < 0 for r in revenue)
        if all_negative:
            return fundamentals

        for i in neg_indices:
            report.add_correction(
                symbol=symbol,
                field="revenue",
                original_value=float(revenue[i]),
                corrected_value=None,
                reason="negative_revenue",
                action="interpolate",
            )

        for i in neg_indices:
            neighbors = []
            if i > 0 and revenue[i - 1] > 0:
                neighbors.append(revenue[i - 1])
            if i < len(revenue) - 1 and revenue[i + 1] > 0:
                neighbors.append(revenue[i + 1])
            if neighbors:
                interpolated = sum(neighbors) / len(neighbors)
                fundamentals.iloc[i, fundamentals.columns.get_loc("revenue")] = interpolated
                for c in report.corrections:
                    if (
                        c.symbol == symbol
                        and c.field == "revenue"
                        and c.reason == "negative_revenue"
                        and c.corrected_value is None
                    ):
                        object.__setattr__(c, "corrected_value", interpolated)

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


class DataValidator:
    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._config = config or ValidationConfig()
        self._rules: list[ValidationRule] = [
            PriceBoundsRule(self._config),
            StaleDataRule(self._config),
        ]

    def validate(
        self,
        df: pd.DataFrame,
        symbol: str,
        report: DataQualityReport,
        fundamentals: pd.DataFrame | None = None,
        sector_median_price: float | None = None,
    ) -> pd.DataFrame:
        kwargs: dict = {}
        if fundamentals is not None:
            kwargs["fundamentals"] = fundamentals
        if sector_median_price is not None:
            kwargs["sector_median_price"] = sector_median_price

        for rule in self._rules:
            df = rule.apply(df, symbol, report, **kwargs)

        if fundamentals is not None:
            ratio_rule = RatioSanityRule(self._config)
            df = ratio_rule.apply(df, symbol, report, **kwargs)

        if sector_median_price is not None:
            gbp_rule = GBpGBPRule(self._config)
            df = gbp_rule.apply(df, symbol, report, **kwargs)

        if report.corrections:
            logger.info(
                "data_quality.corrections_applied",
                symbol=symbol,
                correction_count=len(report.corrections),
                reasons=list({c.reason for c in report.corrections}),
            )

        return df
