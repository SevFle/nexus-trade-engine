"""Concrete historical-data provider backed by CSV files.

Uses :mod:`polars` to read OHLCV CSVs. Required columns (matched
case-insensitively): ``timestamp, open, high, low, close, volume``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import structlog

from engine.data.provider import (
    OHLCV_COLUMNS,
    DataValidationError,
    IDataProvider,
)

logger = structlog.get_logger()

#: Recognised explicit epoch units (and the ``"auto"`` sentinel for the
#: magnitude-based heuristic in :func:`_detect_epoch_unit`).
TIMESTAMP_UNITS: frozenset[str] = frozenset({"auto", "s", "ms", "us", "ns", "ps"})

# Magnitude thresholds for epoch-unit auto-detection. The thresholds are the
# lower bound of the *largest absolute value* in the column that is consistent
# with a plausible (>=1970) UTC datetime for that unit:
#
#   2026-01-01 ≈ 1.77e9 s ≈ 1.77e12 ms ≈ 1.77e15 us ≈ 1.77e18 ns ≈ 1.77e21 ps
#
# Checking from the largest magnitude down keeps each unit in its own band so a
# millisecond column (≈1e12) is never mistaken for nanoseconds, etc.
_PS_THRESHOLD = 1e20
_NS_THRESHOLD = 1e17
_US_THRESHOLD = 1e14
_MS_THRESHOLD = 1e11


class CSVHistoricalDataProvider(IDataProvider):
    """Load historical OHLCV bars from a CSV file using polars.

    The CSV must contain the canonical OHLCV columns defined in
    :data:`engine.data.provider.OHLCV_COLUMNS`. Header names are matched
    case-insensitively (e.g. ``Timestamp`` and ``TIMESTAMP`` are accepted) and
    renamed to the lowercase canonical names. Extra columns are preserved.

    The ``timestamp`` column is coerced to a tz-aware (UTC) polars ``Datetime``:

    * ISO/``strftime`` string timestamps are parsed via ``str.strptime``;
    * integer/float epoch timestamps are interpreted in the unit given by
      ``timestamp_unit`` (seconds, milliseconds, microseconds, nanoseconds or
      picoseconds); and
    * existing ``Date``/``Datetime`` columns are normalised to ``Datetime``.

    Malformed timestamps that fail to parse are reported with the offending row
    values and raise :class:`DataValidationError`.

    Rows are sorted ascending by ``timestamp``.

    Parameters
    ----------
    timestamp_format:
        Optional ``strftime`` pattern used when parsing *string* timestamps
        (e.g. ``"%Y-%m-%d %H:%M:%S"``). When ``None``, polars infers the
        format. Ignored for numeric/date-typed ``timestamp`` columns.
    timestamp_unit:
        Unit of numeric epoch ``timestamp`` columns. One of ``"s"``, ``"ms"``,
        ``"us"``, ``"ns"``, ``"ps"`` or ``"auto"`` (the default). When
        ``"auto"`` (or ``None``) the unit is inferred from the column magnitude
        via :func:`_detect_epoch_unit` and an explicit warning is logged so the
        inference is observable. Ignored for string/date-typed columns.
    """

    name = "csv"

    def __init__(
        self,
        *,
        timestamp_format: str | None = None,
        timestamp_unit: str | None = "auto",
    ) -> None:
        self.timestamp_format = timestamp_format
        self.timestamp_unit = self._normalise_unit(timestamp_unit)

    # -- IDataProvider ---------------------------------------------------

    def validate(self, source: str | Path, **kwargs: Any) -> bool:
        """Return ``True`` if ``source`` has all required OHLCV columns.

        Only the CSV header is read (``n_rows=0``) to keep validation cheap.
        Column names are compared case-insensitively.
        """
        path = Path(source)
        if not path.exists():
            raise DataValidationError(f"CSV file does not exist: {path}")
        try:
            header = pl.read_csv(path, n_rows=0, **kwargs)
        except pl.PolarsError as exc:  # pragma: no cover - defensive
            raise DataValidationError(
                f"Failed to read CSV header from {path}: {exc}",
            ) from exc

        present = {col.lower() for col in header.columns}
        missing = [col for col in OHLCV_COLUMNS if col not in present]
        if missing:
            raise DataValidationError(
                f"CSV {path.name!r} is missing required OHLCV columns: {', '.join(missing)}",
            )
        return True

    def load_data(self, source: str | Path, **kwargs: Any) -> pl.DataFrame:
        """Read the CSV at ``source`` and return a validated polars DataFrame.

        Validates the header first, reads the full file, lowercases column
        names, coerces ``timestamp`` to a tz-aware ``Datetime``, and sorts
        ascending. Extra ``**kwargs`` are forwarded to :func:`polars.read_csv`.
        """
        self.validate(source, **kwargs)
        df = pl.read_csv(source, **kwargs)

        # Normalise to lowercase canonical column names.
        rename_map = {col: col.lower() for col in df.columns if col != col.lower()}
        if rename_map:
            df = df.rename(rename_map)

        df = self._coerce_timestamp(df)
        return df.sort("timestamp")

    # -- helpers ---------------------------------------------------------

    def _coerce_timestamp(self, df: pl.DataFrame) -> pl.DataFrame:
        """Ensure the ``timestamp`` column is a tz-aware polars ``Datetime``.

        Raises :class:`DataValidationError` if any timestamp fails to parse
        (reported with the offending raw values and their row numbers).
        """
        dtype = df.schema["timestamp"]

        # Already a datetime — normalise to UTC tz but keep its unit.
        if isinstance(dtype, pl.Datetime):
            return df.with_columns(_ensure_utc(pl.col("timestamp")))

        # Date -> Datetime(us).
        if dtype == pl.Date:
            return df.with_columns(
                pl.col("timestamp")
                .cast(pl.Datetime("us"))
                .dt.replace_time_zone("UTC"),
            )

        # Numeric column -> epoch (unit from timestamp_unit / auto-detect).
        if _is_numeric(dtype):
            unit = self._resolve_unit(df["timestamp"])
            return df.with_columns(_epoch_to_datetime(pl.col("timestamp"), unit))

        # String -> best-effort parse, then validate no rows dropped to null.
        parsed = df.with_columns(
            pl.col("timestamp").str.strptime(
                pl.Datetime("us"),
                format=self.timestamp_format,
                strict=False,
            ),
        )
        self._raise_on_null_timestamps(parsed, original=df, fmt=self.timestamp_format)
        return parsed

    def _resolve_unit(self, series: pl.Series) -> str:
        """Return the configured epoch unit, auto-detecting when set to ``auto``."""
        if self.timestamp_unit != "auto":
            return self.timestamp_unit
        detected = _detect_epoch_unit(series)
        logger.warning(
            "data_provider.csv.timestamp_unit_auto_detected",
            unit=detected,
            column=series.name,
            hint="pass timestamp_unit=... to CSVHistoricalDataProvider to silence this warning",
        )
        return detected

    @staticmethod
    def _normalise_unit(unit: str | None) -> str:
        if unit is None:
            return "auto"
        norm = unit.strip().lower()
        if norm not in TIMESTAMP_UNITS:
            raise ValueError(
                f"Invalid timestamp_unit {unit!r}; expected one of "
                f"{sorted(TIMESTAMP_UNITS)}.",
            )
        return norm

    @staticmethod
    def _raise_on_null_timestamps(
        parsed: pl.DataFrame,
        *,
        original: pl.DataFrame,
        fmt: str | None,
    ) -> None:
        """Raise :class:`DataValidationError` if any timestamp parsed to null.

        Includes the row number(s) and the offending raw value(s) from the
        *original* (pre-parse) frame so callers can fix the source data.
        """
        nulls = parsed["timestamp"].null_count()
        if nulls == 0:
            return
        # ``parsed`` and ``original`` share the same row order (``with_columns``
        # never reorders), so a positional mask is valid against both frames.
        bad_mask = parsed["timestamp"].is_null() & original["timestamp"].is_not_null()
        positions = [i for i, flag in enumerate(bad_mask.to_list()) if flag]
        raw_values = original["timestamp"].gather(positions).to_list()
        offenders = list(zip(positions, raw_values, strict=True))
        preview_limit = 10
        preview = ", ".join(f"row {i}={v!r}" for i, v in offenders[:preview_limit])
        suffix = (
            "" if len(offenders) <= preview_limit
            else f" (and {len(offenders) - preview_limit} more)"
        )
        raise DataValidationError(
            f"Failed to parse {len(positions)} of {parsed.height} timestamp(s) "
            f"from string column 'timestamp' (format={fmt!r}). "
            f"Offending values: {preview}{suffix}",
        )


def _epoch_to_datetime(col: pl.Expr, unit: str) -> pl.Expr:
    """Convert an epoch numeric column (``unit``) to a UTC ``Datetime(us)``."""
    if unit in ("s", "ms", "us"):
        factor = {"s": 1_000_000, "ms": 1_000, "us": 1}[unit]
        us = col.cast(pl.Int64) * factor
    elif unit == "ns":
        # Integer microseconds (sub-µs precision is irrelevant for OHLCV bars).
        us = col.cast(pl.Int64) // 1_000
    elif unit == "ps":
        # Picosecond magnitudes (≈1e21 for ~2026) overflow Int64, so divide in
        # floating point first, then round back to whole microseconds.
        us = (col.cast(pl.Float64) / 1_000_000.0).round().cast(pl.Int64)
    else:  # pragma: no cover - guarded by _normalise_unit
        raise ValueError(f"Unsupported timestamp_unit: {unit!r}")
    return us.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")


def _ensure_utc(col: pl.Expr) -> pl.Expr:
    """Attach UTC to a naive datetime, leaving aware datetimes untouched."""
    return col.dt.replace_time_zone("UTC")


def _detect_epoch_unit(series: pl.Series) -> str:
    """Infer the epoch unit of a numeric timestamp column by magnitude.

    Uses the largest absolute value in the column:

    * ``>= 1e20`` → ``ps``
    * ``>= 1e17`` → ``ns``
    * ``>= 1e14`` → ``us``
    * ``>= 1e11`` → ``ms``
    * otherwise → ``s``

    Returns ``"s"`` for an empty/all-null column (no magnitude to inspect).
    """
    if series.len() == 0 or series.null_count() == series.len():
        return "s"
    magnitude = float(series.abs().max())
    if magnitude >= _PS_THRESHOLD:
        return "ps"
    if magnitude >= _NS_THRESHOLD:
        return "ns"
    if magnitude >= _US_THRESHOLD:
        return "us"
    if magnitude >= _MS_THRESHOLD:
        return "ms"
    return "s"


def _is_numeric(dtype: pl.DataType) -> bool:
    """True for integer/float polars dtypes."""
    return bool(getattr(dtype, "is_numeric", lambda: False)())


__all__ = ["TIMESTAMP_UNITS", "CSVHistoricalDataProvider"]
