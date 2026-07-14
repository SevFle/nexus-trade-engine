"""Tests for the timestamp-handling code added to ``engine/data/csv_provider.py``.

These cover the *most recently changed* behaviour (commit 8dd3aa7
"fix(csv): Prevent silent data corruption in timestamps"):

* magnitude-based epoch-unit auto-detection (``_detect_epoch_unit``),
* explicit per-unit epoch -> UTC ``Datetime`` conversion (``_epoch_to_datetime``),
* tz-awareness normalisation for existing datetime columns (``_ensure_utc``),
* ``timestamp_unit`` construction/normalisation (``_normalise_unit``),
* the auto-detect structlog *warning* (``_resolve_unit``), and
* strict string parsing that surfaces offending values instead of dropping
  them to null (``_raise_on_null_timestamps``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

import engine.data.csv_provider as csv_mod
from engine.data.csv_provider import (
    TIMESTAMP_UNITS,
    CSVHistoricalDataProvider,
    _detect_epoch_unit,
    _ensure_utc,
    _epoch_to_datetime,
)
from engine.data.provider import DataValidationError

# 2025-01-01 00:00:00 UTC expressed in every supported epoch unit.
_BASE_S = 1_735_689_600
_BASE_PER_UNIT = {
    "s": _BASE_S,
    "ms": _BASE_S * 1_000,
    "us": _BASE_S * 1_000_000,
    "ns": _BASE_S * 1_000_000_000,
    "ps": _BASE_S * 1_000_000_000_000,
}
_UTC = ZoneInfo("UTC")
_EXPECTED_DT = datetime(2025, 1, 1, 0, 0, 0, tzinfo=_UTC)


def _ohlcv_csv(timestamps: list[str]) -> str:
    rows = ["timestamp,open,high,low,close,volume"]
    rows.extend(f"{ts},1.0,2.0,0.5,1.5,100" for ts in timestamps)
    return "\n".join(rows) + "\n"


def _tmp_csv(dir_path: Path, name: str, content: str) -> Path:
    path = dir_path / name
    path.write_text(content)
    return path


# --------------------------------------------------------------------------- #
# _detect_epoch_unit                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns", "ps"])
def test_detect_epoch_unit_recognises_each_realistic_magnitude(unit: str) -> None:
    """A real ~2025 epoch magnitude maps to exactly its own unit."""
    series = pl.Series("timestamp", [_BASE_PER_UNIT[unit]])
    assert _detect_epoch_unit(series) == unit


def test_detect_epoch_unit_uses_largest_absolute_value() -> None:
    """Detection is driven by ``abs().max()``, so negatives are classified too."""
    # -1.7e12 ms magnitude -> ms, not seconds.
    series = pl.Series("timestamp", [-_BASE_PER_UNIT["ms"]])
    assert _detect_epoch_unit(series) == "ms"


def test_detect_epoch_unit_threshold_boundaries_ms_us_ns() -> None:
    """The exact lower bound of each band maps to that unit (>= comparison)."""
    assert _detect_epoch_unit(pl.Series("t", [100_000_000_000])) == "ms"  # 1e11
    assert _detect_epoch_unit(pl.Series("t", [99_999_999_999])) == "s"  # below ms
    assert _detect_epoch_unit(pl.Series("t", [100_000_000_000_000])) == "us"  # 1e14
    assert _detect_epoch_unit(pl.Series("t", [100_000_000_000_000_000])) == "ns"  # 1e17


def test_detect_epoch_unit_empty_column_returns_seconds() -> None:
    """There is no magnitude to inspect, so the lowest unit is the safe default."""
    assert _detect_epoch_unit(pl.Series("timestamp", [])) == "s"


def test_detect_epoch_unit_all_null_column_returns_seconds() -> None:
    """An all-null column has no magnitude, so default to seconds."""
    assert _detect_epoch_unit(pl.Series("timestamp", [None, None])) == "s"


def test_detect_epoch_unit_does_not_confuse_ms_for_ns() -> None:
    """Regression: a millisecond column must never be mistaken for nanoseconds."""
    series = pl.Series("timestamp", [_BASE_PER_UNIT["ms"], _BASE_PER_UNIT["ms"] + 60_000])
    assert _detect_epoch_unit(series) == "ms"


# --------------------------------------------------------------------------- #
# _epoch_to_datetime                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns", "ps"])
def test_epoch_to_datetime_each_unit_converts_to_same_utc_datetime(unit: str) -> None:
    """Every epoch unit of the same instant resolves to one UTC datetime."""
    df = pl.DataFrame({"timestamp": [_BASE_PER_UNIT[unit]]})
    out = df.with_columns(_epoch_to_datetime(pl.col("timestamp"), unit))
    assert out.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert out["timestamp"][0] == _EXPECTED_DT


def test_epoch_to_datetime_output_is_utc_timezone_aware() -> None:
    """The converted column must be tz-aware UTC (the whole point of the fix)."""
    out = pl.DataFrame({"timestamp": [_BASE_S]}).with_columns(
        _epoch_to_datetime(pl.col("timestamp"), "s"),
    )
    assert out.schema["timestamp"].time_zone == "UTC"


# --------------------------------------------------------------------------- #
# _ensure_utc                                                                 #
# --------------------------------------------------------------------------- #


def _datetime_series(value: str, *, tz: str | None) -> pl.DataFrame:
    col = pl.col("timestamp").str.strptime(pl.Datetime("us"))
    if tz is not None:
        col = col.dt.replace_time_zone(tz)
    return pl.DataFrame({"timestamp": [value]}).with_columns(col)


def test_ensure_utc_attaches_utc_to_naive_datetime() -> None:
    df = _datetime_series("2026-01-01", tz=None)
    assert df.schema["timestamp"].time_zone is None
    out = df.with_columns(_ensure_utc(pl.col("timestamp")))
    assert out.schema["timestamp"].time_zone == "UTC"
    assert out["timestamp"][0] == datetime(2026, 1, 1, tzinfo=_UTC)


def test_ensure_utc_leaves_already_utc_datetime_unchanged() -> None:
    df = _datetime_series("2026-01-01", tz="UTC")
    out = df.with_columns(_ensure_utc(pl.col("timestamp")))
    assert out.schema["timestamp"].time_zone == "UTC"
    assert out["timestamp"][0] == datetime(2026, 1, 1, tzinfo=_UTC)


# --------------------------------------------------------------------------- #
# constructor / _normalise_unit                                               #
# --------------------------------------------------------------------------- #


def test_timestamp_units_exports_expected_set() -> None:
    assert frozenset({"auto", "s", "ms", "us", "ns", "ps"}) == TIMESTAMP_UNITS


def test_constructor_default_unit_is_auto() -> None:
    assert CSVHistoricalDataProvider().timestamp_unit == "auto"


def test_constructor_none_unit_normalises_to_auto() -> None:
    assert CSVHistoricalDataProvider(timestamp_unit=None).timestamp_unit == "auto"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("s", "s"),
        ("MS", "ms"),
        ("  Ms  ", "ms"),
        ("Auto", "auto"),
        ("NS", "ns"),
        ("PS", "ps"),
        ("US", "us"),
    ],
)
def test_constructor_normalises_unit_case_and_whitespace(raw: str, expected: str) -> None:
    assert CSVHistoricalDataProvider(timestamp_unit=raw).timestamp_unit == expected


@pytest.mark.parametrize("bad", ["hours", "minutes", "", "day", "kevin"])
def test_constructor_rejects_invalid_unit_with_helpful_message(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid timestamp_unit"):
        CSVHistoricalDataProvider(timestamp_unit=bad)


# --------------------------------------------------------------------------- #
# auto-detect logging (_resolve_unit)                                         #
# --------------------------------------------------------------------------- #


class _RecordingLogger:
    """Stand-in structlog logger that records ``warning`` calls."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append((event, dict(kwargs)))


def test_auto_detect_emits_observable_warning(tmp_path, monkeypatch) -> None:
    """Silent inference is unacceptable: the heuristic must be observable."""
    recorder = _RecordingLogger()
    monkeypatch.setattr(csv_mod, "logger", recorder)

    csv = _tmp_csv(tmp_path, "auto.csv", _ohlcv_csv([str(_BASE_PER_UNIT["ms"])]))
    CSVHistoricalDataProvider().load_data(csv)

    assert len(recorder.warnings) == 1
    event, kwargs = recorder.warnings[0]
    assert event == "data_provider.csv.timestamp_unit_auto_detected"
    assert kwargs["unit"] == "ms"
    assert kwargs["column"] == "timestamp"
    assert "silence" in kwargs["hint"]


@pytest.mark.parametrize("unit", ["ms", "ns", "us"])
def test_explicit_unit_suppresses_auto_detect_warning(tmp_path, monkeypatch, unit: str) -> None:
    """An explicit ``timestamp_unit`` must bypass detection entirely."""
    recorder = _RecordingLogger()
    monkeypatch.setattr(csv_mod, "logger", recorder)

    csv = _tmp_csv(tmp_path, f"{unit}.csv", _ohlcv_csv([str(_BASE_PER_UNIT[unit])]))
    CSVHistoricalDataProvider(timestamp_unit=unit).load_data(csv)

    assert recorder.warnings == []


# --------------------------------------------------------------------------- #
# strict string parsing (_raise_on_null_timestamps)                           #
# --------------------------------------------------------------------------- #


def test_malformed_string_timestamp_raises_with_row_and_value(tmp_path) -> None:
    """The fix's core promise: a bad value is reported, never silently nulled."""
    csv = _tmp_csv(
        tmp_path,
        "bad.csv",
        _ohlcv_csv(["2026-01-01", "not-a-date", "2026-01-03"]),
    )
    with pytest.raises(DataValidationError) as exc:
        CSVHistoricalDataProvider().load_data(csv)

    message = str(exc.value)
    assert "row 1" in message
    assert "'not-a-date'" in message
    assert "Failed to parse 1 of 3" in message


def test_malformed_string_message_includes_format(tmp_path) -> None:
    csv = _tmp_csv(tmp_path, "fmt.csv", _ohlcv_csv(["good", "bad"]))
    with pytest.raises(DataValidationError, match=r"format='%d/%m/%Y'"):
        CSVHistoricalDataProvider(timestamp_format="%d/%m/%Y").load_data(csv)


def test_many_offenders_message_truncates_with_suffix(tmp_path) -> None:
    """More than the preview limit of offending values gets a 'more' suffix."""
    timestamps = ["2026-01-01", *(f"bad{i}" for i in range(12))]
    csv = _tmp_csv(tmp_path, "many.csv", _ohlcv_csv(timestamps))
    with pytest.raises(DataValidationError) as exc:
        CSVHistoricalDataProvider().load_data(csv)

    assert "(and 2 more)" in str(exc.value)


def test_all_valid_strings_do_not_raise(tmp_path) -> None:
    csv = _tmp_csv(tmp_path, "ok.csv", _ohlcv_csv(["2026-01-01", "2026-01-02"]))
    df = CSVHistoricalDataProvider().load_data(csv)
    assert df["timestamp"].null_count() == 0
    assert df.shape == (2, 6)


def test_explicit_timestamp_format_applied(tmp_path) -> None:
    """A user-supplied strftime format must drive string parsing."""
    csv = _tmp_csv(
        tmp_path,
        "ddmmyyyy.csv",
        _ohlcv_csv(["01/01/2026", "02/01/2026"]),
    )
    df = CSVHistoricalDataProvider(timestamp_format="%d/%m/%Y").load_data(csv)
    # The string-parsed path keeps the parsed calendar dates (day/month first).
    parsed = df["timestamp"].to_list()
    assert [d.year for d in parsed] == [2026, 2026]
    assert [d.month for d in parsed] == [1, 1]
    assert [d.day for d in parsed] == [1, 2]
    assert df["timestamp"].null_count() == 0


def test_empty_timestamp_cell_raises_rather_than_silent_null(tmp_path) -> None:
    """An empty cell parses to null; the strict guard surfaces it as an error."""
    content = "timestamp,open,high,low,close,volume\n2026-01-01,1,2,0,1,10\n,1,2,0,1,10\n"
    csv = _tmp_csv(tmp_path, "empty.csv", content)
    with pytest.raises(DataValidationError, match="Failed to parse"):
        CSVHistoricalDataProvider().load_data(csv)


# --------------------------------------------------------------------------- #
# full provider integration across timestamp column dtypes                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("unit", "value"),
    [("s", _BASE_S), ("ms", _BASE_PER_UNIT["ms"]), ("ns", _BASE_PER_UNIT["ns"])],
)
def test_numeric_epoch_columns_use_explicit_unit(tmp_path, unit: str, value: int) -> None:
    csv = _tmp_csv(tmp_path, f"{unit}.csv", _ohlcv_csv([str(value)]))
    df = CSVHistoricalDataProvider(timestamp_unit=unit).load_data(csv)
    assert df.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert df["timestamp"][0] == _EXPECTED_DT


def test_numeric_seconds_auto_detected(tmp_path) -> None:
    csv = _tmp_csv(tmp_path, "sec.csv", _ohlcv_csv([str(_BASE_S)]))
    df = CSVHistoricalDataProvider().load_data(csv)
    assert df["timestamp"][0] == _EXPECTED_DT


def test_float_epoch_column_is_treated_as_numeric(tmp_path) -> None:
    """Float epoch columns (no decimal noise) must hit the numeric path."""
    csv = _tmp_csv(tmp_path, "float.csv", _ohlcv_csv([str(float(_BASE_PER_UNIT["ms"]))]))
    df = CSVHistoricalDataProvider(timestamp_unit="ms").load_data(csv)
    assert df["timestamp"][0] == _EXPECTED_DT


def test_date_typed_column_becomes_utc_datetime(tmp_path) -> None:
    csv = _tmp_csv(tmp_path, "date.csv", _ohlcv_csv(["2026-01-01"]))
    df = CSVHistoricalDataProvider().load_data(csv, schema_overrides={"timestamp": pl.Date})
    assert df.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert df["timestamp"][0] == datetime(2026, 1, 1, tzinfo=_UTC)


def test_existing_datetime_column_gets_utc_timezone(tmp_path) -> None:
    csv = _tmp_csv(tmp_path, "datetime.csv", _ohlcv_csv(["2026-01-01"]))
    df = CSVHistoricalDataProvider().load_data(
        csv, schema_overrides={"timestamp": pl.Datetime("us")}
    )
    assert df.schema["timestamp"].time_zone == "UTC"
    assert df["timestamp"][0] == datetime(2026, 1, 1, tzinfo=_UTC)


def test_numeric_out_of_order_rows_sorted_ascending(tmp_path) -> None:
    csv = _tmp_csv(
        tmp_path,
        "ooo.csv",
        _ohlcv_csv([str(_BASE_S + 86400), str(_BASE_S)]),
    )
    df = CSVHistoricalDataProvider(timestamp_unit="s").load_data(csv)
    assert df["timestamp"].to_list() == [
        datetime(2025, 1, 1, tzinfo=_UTC),
        datetime(2025, 1, 2, tzinfo=_UTC),
    ]


# --------------------------------------------------------------------------- #
# header validation / normalisation                                          #
# --------------------------------------------------------------------------- #


def test_missing_ohlcv_column_is_rejected(tmp_path) -> None:
    csv = _tmp_csv(
        tmp_path,
        "incomplete.csv",
        "timestamp,open,close,volume\n2026-01-01,1.0,1.5,100\n",
    )
    with pytest.raises(DataValidationError, match="missing required OHLCV columns"):
        CSVHistoricalDataProvider().validate(csv)


def test_uppercase_headers_normalised_to_canonical_names(tmp_path) -> None:
    csv = _tmp_csv(
        tmp_path,
        "upper.csv",
        "TIMESTAMP,OPEN,HIGH,LOW,CLOSE,VOLUME\n2026-01-01,1.0,2.0,0.5,1.5,100\n",
    )
    df = CSVHistoricalDataProvider().load_data(csv)
    assert list(df.columns) == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
