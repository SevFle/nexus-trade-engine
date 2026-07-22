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

import asyncio
import time
import weakref
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
        ttl: Cache time-to-live in seconds. **Takes precedence** over
            ``ttl_seconds`` when both are given. Defaults to 60s. ``0``
            disables serving from cache (every call delegates). Negative
            values are rejected.
        ttl_seconds: Alias for ``ttl``; consulted only when ``ttl`` is
            ``None``. Kept for symmetry with the underlying provider
            surface and for callers that prefer the more explicit name.

    Raises:
        ValueError: If the resolved TTL is negative.
    """

    def __init__(
        self,
        provider: IDataProvider,
        ttl: float | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        # ``ttl`` is the canonical kwarg and wins over the ``ttl_seconds``
        # alias; when neither is supplied the documented 60s default applies.
        if ttl is not None:
            effective_ttl = ttl
        elif ttl_seconds is not None:
            effective_ttl = ttl_seconds
        else:
            effective_ttl = DEFAULT_TTL_SECONDS
        if effective_ttl < 0:
            raise ValueError(f"ttl must be non-negative, got {effective_ttl}")
        self._provider = provider
        self._ttl = float(effective_ttl)
        # key (symbol, interval, date_range) -> (stored_at_monotonic, value)
        self._cache: dict[tuple[Any, Any, Any], tuple[float, Any]] = {}
        # Per-key single-flight locks. When N coroutines request the same
        # key concurrently only the first one misses and fetches from the
        # wrapped provider; the rest await the lock and then observe the
        # freshly stored value (see :meth:`_lock_for`). A
        # :class:`weakref.WeakValueDictionary` is used deliberately: once
        # no coroutine is holding/awaiting a lock it is garbage-collected,
        # so the mapping cannot grow unboundedly as distinct keys churn
        # through the cache over the process lifetime.
        self._key_locks: weakref.WeakValueDictionary[Any, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

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
        """Drop every cached entry. Mainly used between tests.

        Only ``_cache`` is reset: ``_key_locks`` is a
        :class:`weakref.WeakValueDictionary` whose entries are reclaimed
        automatically once no coroutine holds a lock, so it needs no
        explicit clearing (and clearing it could drop a lock that a
        concurrent coroutine is still waiting on, breaking single-flight).
        """
        self._cache.clear()

    # ------------------------------------------------------------------ #
    # transparent delegation for the rest of the IDataProvider surface
    # ------------------------------------------------------------------ #
    def __getattr__(self, name: str) -> Any:
        """Forward any uncached attribute to the wrapped provider.

        ``__getattr__`` is only invoked when normal (``__dict__`` + class)
        lookup fails, so explicitly-defined members — ``get_ohlcv``,
        ``get_latest_price``, ``provider``, ``ttl``, ``clear``, the private
        helpers — are served directly and never reach here. Any *other*
        attribute (e.g. ``health_check``, ``get_multiple_prices``,
        ``stream_prices``) is forwarded to ``self._provider`` so the wrapper
        is a transparent, drop-in decorator for the parts of the
        :class:`IDataProvider` surface it does not cache.

        Dunder and underscore-prefixed names are deliberately **not**
        forwarded: the copy/pickle protocols probe for hooks such as
        ``__deepcopy__``, ``__copy__``, ``__getstate__`` and ``__reduce_ex__``
        via ``getattr(obj, name, default)``. Forwarding them would let a mock
        provider (which auto-creates any attribute) appear to implement those
        hooks, or would leak the wrapped provider's own (de)serialisation
        machinery onto the wrapper. Raising :class:`AttributeError` instead
        makes the protocols fall back to their own defaults, so ``hasattr`` /
        ``getattr(..., default)`` behave sanely.

        ``self.__dict__`` is read directly rather than going through
        ``self._provider`` so that, if ``_provider`` is not yet set — e.g.
        during unpickling or :func:`copy.copy` before ``__init__`` runs —
        we raise :class:`AttributeError` instead of recursing back into
        ``__getattr__`` for ``_provider`` itself.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        provider = self.__dict__.get("_provider")
        if provider is None:
            raise AttributeError(name)
        return getattr(provider, name)

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

    def _lock_for(self, key: Any) -> asyncio.Lock:
        """Return the per-key single-flight :class:`asyncio.Lock`.

        Because ``asyncio`` is cooperative, two coroutines cannot
        interleave inside this synchronous method. At most one
        :class:`~asyncio.Lock` is therefore created per key and shared by
        every concurrent caller, serialising the check-then-fetch-then-
        store critical section in the cached methods (preventing a
        "thundering herd" of identical upstream fetches).

        The mapping is a :class:`weakref.WeakValueDictionary`, so the
        lookup uses an explicit get-then-create-then-set sequence rather
        than ``setdefault``: we must hold a strong reference to the freshly
        created lock in a local variable *before* storing it, otherwise the
        weak entry could be reclaimed between insertion and return. Once
        the caller's ``async with`` releases the lock and drops the last
        strong reference, the entry is garbage-collected and the mapping
        shrinks — the dict therefore cannot grow unboundedly as distinct
        keys churn through the cache.
        """
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

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
        period: str | None = None,
        date_range: str | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return cached OHLCV bars or fetch them from the wrapped provider.

        ``period`` is the canonical window selector and **takes precedence**
        over the legacy ``date_range`` alias: if both are given ``period``
        wins, otherwise whichever is supplied is used (when neither is given
        the window is ``None`` and is passed straight through to the wrapped
        provider). The resolved window becomes the third slot of the cache
        key and is forwarded to the wrapped provider's
        ``get_ohlcv(period=…)`` call.

        Cache key: ``(symbol, interval, resolved_window)``.

        The check-then-fetch-then-store critical section is guarded by a
        per-key :class:`asyncio.Lock` so that ``N`` concurrent requests for
        the *same* key collapse into a single upstream fetch (single-flight).
        The exact stored object is returned by reference on every call
        (hit or miss): callers rely on identity equality between repeated
        lookups, so no defensive copy is made. Callers that need to mutate
        the frame should copy it themselves.
        """
        effective_period = period if period is not None else date_range
        key: tuple[Any, Any, Any] = (symbol, interval, effective_period)
        async with self._lock_for(key):
            hit = self._lookup(key)
            if hit is not _MISS:
                logger.debug(
                    "data_provider.cache.hit",
                    method="get_ohlcv",
                    symbol=symbol,
                    interval=interval,
                    period=effective_period,
                )
                return hit

            logger.debug(
                "data_provider.cache.miss",
                method="get_ohlcv",
                symbol=symbol,
                interval=interval,
                period=effective_period,
            )
            value = await self._provider.get_ohlcv(
                symbol, period=effective_period, interval=interval
            )
            self._store(key, value)
            return value

    async def get_latest_price(self, symbol: str) -> float | None:
        """Return cached latest price or fetch it from the wrapped provider.

        Cache key: ``(symbol, None, None)`` — price lookups are not
        parameterised by interval/date_range, so those key slots are
        ``None``. A cached ``None`` ("no price available") is served from
        cache like any other value to avoid repeatedly asking an upstream
        that has nothing to return.

        As with :meth:`get_ohlcv`, the critical section is guarded by a
        per-key lock so concurrent requests for the same symbol make at
        most one upstream call within the window. Prices are immutable
        scalars so no defensive copy is required here.
        """
        key: tuple[Any, Any, Any] = (symbol, None, None)
        async with self._lock_for(key):
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
