"""
Clock abstraction for execution backends.

Provides a pluggable time source so backends can use wall-clock time
(paper/live) or simulated time (backtest) without hard-coding Date.now()
or time.monotonic() calls.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class IClock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        ...

    @abstractmethod
    def monotonic(self) -> float:
        ...


class SystemClock(IClock):
    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class SimulatedClock(IClock):
    def __init__(
        self,
        start: datetime | None = None,
        mono: float = 0.0,
    ) -> None:
        self._dt = start or datetime.now(tz=UTC)
        self._mono = mono

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._dt += timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, dt: datetime, mono: float | None = None) -> None:
        self._dt = dt
        if mono is not None:
            self._mono = mono
