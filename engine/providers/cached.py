"""In-memory TTL cache wrapper for data providers.

:class:`CachedDataProvider` wraps any :class:`IDataProvider` and serves
``get_ohlcv`` / ``get_latest_price`` responses from an in-memory cache keyed
by ``(symbol, interval, date_range)``. When a cache entry is missing — or
older than the configured TTL — the call is delegated to the wrapped
provider and the result is stored for subsequent lookups.

This is intentionally a *process-local, non-distributed* cache. It exists to
avoid hammering a remote upstream with identical OHLCV/price requests within
a short window (e.g. repeated backtest parameter sweeps, dashboard
polling). For cross-process sharing use the Redis/Valkey-backed
:class:`engine.data.providers._cache.ProviderCache` that the HTTP adapters
already layer underneath.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import pandas as pd

    from engine.data.providers.base import IDataProvider

logger = structlog.get_logger()

#: Default time-to-live, in seconds, for cached responses.
DEFAULT_TTL_SECONDS: float = 60.0


class _Miss:
    """Sentinel distinguishing "no cache entry" from a cached ``None``.

    ``get_latest_price`` legitimately returns ``None`` ("no price right
    now"); without a sentinel we could not tell a *cached* ``None`` (which
    should be served) from an absent entry (which must be fetched).
    """


_MISS: Any = _Miss()


class CachedDataProvider:
    """TTL-caching decorator over :class:`IDataProvider`.

    The cache is a plain ``dict`` mapping a ``(symbol, interval, date_range)``
    tuple to a ``(stored_at, value)`` pair, where ``stored_at`` is a
    :func:`time.monotonic` reading. Monotonic time is used deliberately so
    cache freshness cannot be skewed by wall-clock adjustments (NTP jumps,
    DST transitions).

    Behaviour:

    * **Fresh hit** — entry exists and ``monotonic() - stored_at < ttl``:
      return the stored value without touching the wrapped provider.
    * **Miss / stale** — entry absent or past TTL: delegate to the wrapped
      provider and store the result (even if it is ``None`` or an empty
      frame) so a "provider returned nothing" is itself cached and avoids
      hammering an upstream that has no data for a symbol.

    Args:
        provider: The wrapped :class:`IDataProvider` to delegate to on a
            cache miss.
        ttl: Cache time-to-live in seconds. Defaults to 60s. ``0`` disables
            serving from cache (every call delegates). Negative values are
            rejected.

    Raises:
        ValueError: If ``ttl`` is negative.
    """

    def __init__(
        self,
        provider: IDataProvider,
        ttl: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl < 0:
            raise ValueError(f"ttl must be non-negative, got {ttl}")
        self._provider = provider
        self._ttl = float(ttl)
        # key (symbol, interval, date_range) -> (stored_at_monotonic, value)
        self._cache: dict[tuple[Any, Any, Any], tuple[float, Any]] = {}

    # ------------------------------------------------------------------ #
    # read-only surface, mainly for tests / introspection
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> IDataProvider:
        """The wrapped underlying provider."""
        return self._provider

    @property
    def ttl(self) -> float:
        """Configured cache TTL, in seconds."""
        return self._ttl

    def clear(self) -> None:
        """Drop every cached entry. Mainly used between tests."""
        self._cache.clear()

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #
    def _is_fresh(self, stored_at: float, now: float) -> bool:
        # A TTL of 0 means "never serve from cache" — always re-fetch.
        if self._ttl == 0:
            return False
        return (now - stored_at) < self._ttl

    def _lookup(self, key: tuple[Any, Any, Any]) -> Any:
        """Return a cached value if fresh, else the :data:`_MISS` sentinel."""
        entry = self._cache.get(key)
        if entry is None:
            return _MISS
        stored_at, value = entry
        if self._is_fresh(stored_at, time.monotonic()):
            return value
        # Stale: evict so the dict cannot grow unbounded on churn.
        self._cache.pop(key, None)
        return _MISS

    def _store(self, key: tuple[Any, Any, Any], value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)

    def _age_all(self, seconds: float) -> None:
        """Subtract ``seconds`` from every entry's stored timestamp.

        Test-only helper that simulates the passage of wall-clock time
        without patching :func:`time.monotonic`. It deliberately reaches
        into the private cache structure so tests can exercise TTL expiry
        deterministically and instantly.
        """
        for key, (stored_at, value) in list(self._cache.items()):
            self._cache[key] = (stored_at - seconds, value)

    # ------------------------------------------------------------------ #
    # IDataProvider surface (subset)
    # ------------------------------------------------------------------ #
    async def get_ohlcv(
        self,
        symbol: str,
        date_range: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return cached OHLCV bars or fetch them from the wrapped provider.

        Cache key: ``(symbol, interval, date_range)``. The wrapped provider's
        ``get_ohlcv`` is invoked with ``period=date_range`` so the cached
        call matches the underlying :class:`IDataProvider` signature.
        """
        key: tuple[Any, Any, Any] = (symbol, interval, date_range)
        hit = self._lookup(key)
        if hit is not _MISS:
            logger.debug(
                "data_provider.cache.hit",
                method="get_ohlcv",
                symbol=symbol,
                interval=interval,
                date_range=date_range,
            )
            return hit

        logger.debug(
            "data_provider.cache.miss",
            method="get_ohlcv",
            symbol=symbol,
            interval=interval,
            date_range=date_range,
        )
        value = await self._provider.get_ohlcv(symbol, period=date_range, interval=interval)
        self._store(key, value)
        return value

    async def get_latest_price(self, symbol: str) -> float | None:
        """Return cached latest price or fetch it from the wrapped provider.

        Cache key: ``(symbol, None, None)`` — price lookups are not
        parameterised by interval/date_range, so those key slots are
        ``None``. A cached ``None`` ("no price available") is served from
        cache like any other value to avoid repeatedly asking an upstream
        that has nothing to return.
        """
        key: tuple[Any, Any, Any] = (symbol, None, None)
        hit = self._lookup(key)
        if hit is not _MISS:
            logger.debug(
                "data_provider.cache.hit",
                method="get_latest_price",
                symbol=symbol,
            )
            return hit

        logger.debug(
            "data_provider.cache.miss",
            method="get_latest_price",
            symbol=symbol,
        )
        value = await self._provider.get_latest_price(symbol)
        self._store(key, value)
        return value


__all__ = ["DEFAULT_TTL_SECONDS", "CachedDataProvider"]
