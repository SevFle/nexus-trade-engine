"""Market calendar: trading sessions, holidays, half-days per venue.

Pure-stdlib (datetime + zoneinfo). No pandas-market-calendars dep.

Per-venue calendar carries:
- timezone (IANA zone name)
- regular open + close (local time)
- holiday set (full closures, by local date)
- half-day map (early-close override, by local date)

Public functions are stateless — they take a :class:`VenueCalendar`
and a datetime/date and answer the schedule question. The
:class:`MarketCalendar` service is a thin facade over the built-in
catalog for callers that want a stable handle.

Built-in calendars cover the major liquidity venues:
- XNAS (NYSE / Nasdaq, America/New_York, 09:30-16:00)
- XLON (London Stock Exchange, Europe/London, 08:00-16:30)
- XHKG (Hong Kong, Asia/Hong_Kong, 09:30-16:00)
- XTKS (Tokyo, Asia/Tokyo, 09:00-15:00)
- XETR (Frankfurt Xetra, Europe/Berlin, 09:00-17:30)

Holiday tables ship empty by default — operators load the venue's
published schedule via the corporate-actions ingestion job (#112).
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo


_MIC_LENGTH = 4
_WEEKEND_START = 5  # Saturday
_MAX_LOOKAHEAD_DAYS = 366


class MarketCalendarError(Exception):
    """Raised on malformed venue calendars or unknown MIC codes."""


@dataclass(frozen=True)
class VenueCalendar:
    """Trading session schedule for a single venue."""

    mic: str
    timezone: str
    regular_open: time
    regular_close: time
    holidays: frozenset[date] = field(default_factory=frozenset)
    half_days: dict[date, time] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.mic) != _MIC_LENGTH or not self.mic.isalnum():
            msg = f"mic must be 4 alphanumeric chars; got {self.mic!r}"
            raise MarketCalendarError(msg)
        if self.regular_open >= self.regular_close:
            msg = (
                f"regular_open {self.regular_open} must precede "
                f"regular_close {self.regular_close}"
            )
            raise MarketCalendarError(msg)
        try:
            zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"invalid timezone {self.timezone!r}: {exc}"
            raise MarketCalendarError(msg) from exc

    @property
    def zone(self) -> ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)


@dataclass(frozen=True)
class SessionBounds:
    """Open/close datetimes for a single trading day."""

    open_dt: datetime
    close_dt: datetime


def _to_local(cal: VenueCalendar, dt: datetime) -> datetime:
    if dt.tzinfo is None:
        msg = "datetime must be timezone-aware"
        raise MarketCalendarError(msg)
    return dt.astimezone(cal.zone)


def _is_trading_day(cal: VenueCalendar, day: date) -> bool:
    if day.weekday() >= _WEEKEND_START:
        return False
    return day not in cal.holidays


def _close_for_day(cal: VenueCalendar, day: date) -> time:
    half = cal.half_days.get(day)
    return half if half is not None else cal.regular_close


def is_open(cal: VenueCalendar, dt: datetime) -> bool:
    """Return True iff ``dt`` falls inside a regular or half-day session."""
    local = _to_local(cal, dt)
    if not _is_trading_day(cal, local.date()):
        return False
    close = _close_for_day(cal, local.date())
    return cal.regular_open <= local.time() < close


def session_bounds(
    cal: VenueCalendar, day: date
) -> SessionBounds | None:
    """Return open/close datetimes for ``day``, or ``None`` if closed."""
    if not _is_trading_day(cal, day):
        return None
    close = _close_for_day(cal, day)
    open_dt = datetime.combine(day, cal.regular_open, tzinfo=cal.zone)
    close_dt = datetime.combine(day, close, tzinfo=cal.zone)
    return SessionBounds(open_dt=open_dt, close_dt=close_dt)


def next_open(cal: VenueCalendar, dt: datetime) -> datetime:
    """Return the next datetime at which the venue is open.

    If ``dt`` already falls inside a session, returns ``dt`` itself.
    Otherwise advances day-by-day until a trading day with a session
    that hasn't yet closed is found.
    """
    local = _to_local(cal, dt)
    cursor_date = local.date()
    cursor_time = local.time()
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        bounds = session_bounds(cal, cursor_date)
        if bounds is not None:
            local_open = bounds.open_dt.time()
            local_close = bounds.close_dt.time()
            if cursor_time < local_open:
                return bounds.open_dt
            if cursor_time < local_close:
                return datetime.combine(
                    cursor_date, cursor_time, tzinfo=cal.zone
                )
        cursor_date += timedelta(days=1)
        cursor_time = time(0, 0)
    msg = (
        f"no open session found within {_MAX_LOOKAHEAD_DAYS} days of "
        f"{dt!r} for venue {cal.mic}; check holiday table"
    )
    raise MarketCalendarError(msg)


_BUILTIN: dict[str, VenueCalendar] = {
    "XNAS": VenueCalendar(
        mic="XNAS",
        timezone="America/New_York",
        regular_open=time(9, 30),
        regular_close=time(16, 0),
    ),
    "XNYS": VenueCalendar(
        mic="XNYS",
        timezone="America/New_York",
        regular_open=time(9, 30),
        regular_close=time(16, 0),
    ),
    "XLON": VenueCalendar(
        mic="XLON",
        timezone="Europe/London",
        regular_open=time(8, 0),
        regular_close=time(16, 30),
    ),
    "XHKG": VenueCalendar(
        mic="XHKG",
        timezone="Asia/Hong_Kong",
        regular_open=time(9, 30),
        regular_close=time(16, 0),
    ),
    "XTKS": VenueCalendar(
        mic="XTKS",
        timezone="Asia/Tokyo",
        regular_open=time(9, 0),
        regular_close=time(15, 0),
    ),
    "XETR": VenueCalendar(
        mic="XETR",
        timezone="Europe/Berlin",
        regular_open=time(9, 0),
        regular_close=time(17, 30),
    ),
}


def builtin_calendar(mic: str) -> VenueCalendar:
    cal = _BUILTIN.get(mic)
    if cal is None:
        msg = f"no built-in calendar for MIC {mic!r}"
        raise MarketCalendarError(msg)
    return cal


class MarketCalendar:
    """Stable facade over the built-in catalog with override hook."""

    def __init__(
        self, *, overrides: dict[str, VenueCalendar] | None = None
    ) -> None:
        self._overrides = dict(overrides or {})

    def for_venue(self, mic: str) -> VenueCalendar:
        if mic in self._overrides:
            return self._overrides[mic]
        return builtin_calendar(mic)


__all__ = [
    "MarketCalendar",
    "MarketCalendarError",
    "SessionBounds",
    "VenueCalendar",
    "builtin_calendar",
    "is_open",
    "next_open",
    "session_bounds",
]
