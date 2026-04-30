"""Tests for engine.core.market_calendar — venue trading sessions."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from engine.core.market_calendar import (
    MarketCalendar,
    MarketCalendarError,
    SessionBounds,
    VenueCalendar,
    builtin_calendar,
    is_open,
    next_open,
    session_bounds,
)

_NYC = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


def _xnas() -> VenueCalendar:
    return builtin_calendar("XNAS")


class TestBuiltinCalendars:
    def test_xnas_known(self):
        cal = builtin_calendar("XNAS")
        assert cal.mic == "XNAS"
        assert cal.timezone == "America/New_York"

    def test_xlon_known(self):
        cal = builtin_calendar("XLON")
        assert cal.mic == "XLON"
        assert cal.timezone == "Europe/London"

    def test_unknown_mic_raises(self):
        with pytest.raises(MarketCalendarError):
            builtin_calendar("NOTAVENUE")


class TestIsOpen:
    def test_regular_weekday_at_noon_open(self):
        cal = _xnas()
        dt = datetime(2024, 1, 9, 12, 0, tzinfo=_NYC)
        assert is_open(cal, dt) is True

    def test_saturday_closed(self):
        cal = _xnas()
        dt = datetime(2024, 1, 6, 12, 0, tzinfo=_NYC)
        assert is_open(cal, dt) is False

    def test_sunday_closed(self):
        cal = _xnas()
        dt = datetime(2024, 1, 7, 12, 0, tzinfo=_NYC)
        assert is_open(cal, dt) is False

    def test_pre_market_closed(self):
        cal = _xnas()
        dt = datetime(2024, 1, 9, 8, 30, tzinfo=_NYC)
        assert is_open(cal, dt) is False

    def test_after_close_closed(self):
        cal = _xnas()
        dt = datetime(2024, 1, 9, 16, 30, tzinfo=_NYC)
        assert is_open(cal, dt) is False

    def test_holiday_closed(self):
        cal = VenueCalendar(
            mic="XCST",
            timezone="UTC",
            regular_open=time(9, 30),
            regular_close=time(16, 0),
            holidays=frozenset({date(2024, 12, 25)}),
        )
        dt = datetime(2024, 12, 25, 12, 0, tzinfo=_UTC)
        assert is_open(cal, dt) is False

    def test_half_day_uses_early_close(self):
        cal = VenueCalendar(
            mic="XCST",
            timezone="UTC",
            regular_open=time(9, 30),
            regular_close=time(16, 0),
            half_days={date(2024, 11, 29): time(13, 0)},
        )
        dt = datetime(2024, 11, 29, 14, 0, tzinfo=_UTC)
        assert is_open(cal, dt) is False
        dt = datetime(2024, 11, 29, 12, 0, tzinfo=_UTC)
        assert is_open(cal, dt) is True


class TestNextOpen:
    def test_during_session_returns_now(self):
        cal = _xnas()
        dt = datetime(2024, 1, 9, 12, 0, tzinfo=_NYC)
        out = next_open(cal, dt)
        assert out == dt

    def test_pre_market_returns_today_open(self):
        cal = _xnas()
        dt = datetime(2024, 1, 9, 8, 0, tzinfo=_NYC)
        out = next_open(cal, dt)
        assert out == datetime(2024, 1, 9, 9, 30, tzinfo=_NYC)

    def test_after_close_returns_next_day_open(self):
        cal = _xnas()
        dt = datetime(2024, 1, 9, 17, 0, tzinfo=_NYC)
        out = next_open(cal, dt)
        assert out == datetime(2024, 1, 10, 9, 30, tzinfo=_NYC)

    def test_friday_after_close_skips_to_monday(self):
        cal = _xnas()
        dt = datetime(2024, 1, 5, 17, 0, tzinfo=_NYC)
        out = next_open(cal, dt)
        assert out == datetime(2024, 1, 8, 9, 30, tzinfo=_NYC)


class TestSessionBounds:
    def test_regular_day_bounds(self):
        cal = _xnas()
        out = session_bounds(cal, date(2024, 1, 9))
        assert isinstance(out, SessionBounds)
        assert out.open_dt == datetime(2024, 1, 9, 9, 30, tzinfo=_NYC)
        assert out.close_dt == datetime(2024, 1, 9, 16, 0, tzinfo=_NYC)

    def test_weekend_bounds_none(self):
        cal = _xnas()
        out = session_bounds(cal, date(2024, 1, 6))
        assert out is None

    def test_half_day_bounds_use_early_close(self):
        cal = VenueCalendar(
            mic="XCST",
            timezone="UTC",
            regular_open=time(9, 30),
            regular_close=time(16, 0),
            half_days={date(2024, 11, 29): time(13, 0)},
        )
        out = session_bounds(cal, date(2024, 11, 29))
        assert out is not None
        assert out.close_dt.time() == time(13, 0)


class TestServiceFacade:
    def test_calendar_service_resolve(self):
        svc = MarketCalendar()
        cal = svc.for_venue("XNAS")
        assert cal.mic == "XNAS"


class TestValidation:
    def test_open_must_precede_close(self):
        with pytest.raises(MarketCalendarError):
            VenueCalendar(
                mic="XCST",
                timezone="UTC",
                regular_open=time(16, 0),
                regular_close=time(9, 30),
            )

    def test_invalid_mic_format_rejected(self):
        with pytest.raises(MarketCalendarError):
            VenueCalendar(
                mic="ABC",
                timezone="UTC",
                regular_open=time(9, 30),
                regular_close=time(16, 0),
            )

    def test_invalid_timezone_rejected(self):
        with pytest.raises(MarketCalendarError):
            VenueCalendar(
                mic="XCST",
                timezone="Not/A/Real/Zone",
                regular_open=time(9, 30),
                regular_close=time(16, 0),
            )


class TestUtcArgsAccepted:
    def test_is_open_with_utc_arg_converts_to_local(self):
        cal = _xnas()
        # 17:00 UTC = 12:00 EST on 2024-01-09.
        dt = datetime(2024, 1, 9, 17, 0, tzinfo=_UTC)
        assert is_open(cal, dt) is True
