"""Transparent TTL cache wrapper for any :class:`IDataProvider`.

:class:`CachedDataProvider` is a *transparent* decorator: it implements the
:class:`~engine.data.providers.base.IDataProvider` surface by delegating
every call to the wrapped provider, but it memoizes the expensive
``get_ohlcv`` / ``get_instruments`` responses in an in-memory cache keyed
by the call arguments. A cached entry is served as long as it is younger
than the configured time-to-live; once it expires, the next call is
delegated to the wrapped provider and the fresh result is stored.

This is intentionally a *process-local, non-distributed* cache. It exists
to avoid hammering a remote upstream with identical OHLCV/instrument
requests within a short window (repeated backtest sweeps, dashboard
polling). For cross-process sharing use the Redis/Valkey-backed
:class:`engine.data.providers._cache.ProviderCache` that the HTTP adapters
already layer underneath.

Design notes:

* **Monotonic time** — freshness is measured with :func:`time.monotonic` so
  NTP jumps, wall-clock skew, and DST transitions cannot artificially
  extend or shrink an entry's lifetime.
* **Sentinel miss** — ``_MISS`` distinguishes "no cache entry" from a
  cached falsy value (``None``, empty frame). Without it a cached ``None``
  ("no data right now") would be indistinguishable from an absent entry
  and every polling tick would re-hit the upstream.
* **Transparent delegation** — methods that are *not* cached (e.g.
  ``get_latest_price``, ``stream_prices``, ``health_check``) fall through
  :meth:`__getattr__` to the wrapped provider unchanged, so the wrapper is
  a drop-in replacement anywhere an :class:`IDataProvider` is expected.
"""

from __future__ import annotations

import asyncio
import json
import time
import weakref
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers

    import pandas as pd

    from engine.data.providers.base import (
        IDataProvider,
    )

logger = structlog.get_logger()

#: Default time-to-live, in seconds, for cached responses.
DEFAULT_TTL_SECONDS: float = 60.0


class _Miss:
    """Sentinel distinguishing "no cache entry" from a cached ``None``.

    ``get_ohlcv`` may legitimately return an empty frame and
    ``get_instruments`` may return ``[]`` ("nothing trades here"). Without
    a sentinel those falsy cached values would be indistinguishable from an
    absent entry and every subsequent call would re-fetch from the wrapped
    provider, defeating the cache.
    """


_MISS: Any = _Miss()


class CachedDataProvider:
    """Transparent TTL-caching wrapper around an :class:`IDataProvider`.

    Behaviour per cached method:

    * **Fresh hit** — entry exists and ``monotonic() - stored_at < ttl``:
      return the stored value without touching the wrapped provider.
    * **Miss / stale** — entry absent or past TTL: delegate to the wrapped
      provider and store the result (even if it is ``None`` or an empty
      collection) so a "provider returned nothing" is itself cached and
      avoids hammering an upstream that has no data.

    Non-cached methods (everything on :class:`IDataProvider` other than
    ``get_ohlcv`` / ``get_instruments``) are forwarded verbatim to the
    wrapped provider via :meth:`__getattr__`, so the wrapper behaves
    exactly like the underlying provider from a caller's perspective.

    Args:
        provider: The wrapped :class:`IDataProvider` to delegate to on a
            cache miss.
        ttl_seconds: Cache time-to-live in seconds. Defaults to 60s.
            ``0`` disables serving from cache (every call delegates).
            Negative values are rejected.

    Raises:
        ValueError: If ``ttl_seconds`` is negative.
    """

    def __init__(
        self,
        provider: IDataProvider,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be non-negative, got {ttl_seconds}")
        # Use object.__setattr__ to set _provider before any __getattr__
        # machinery could fire — keeps __getattr__ safe during init.
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_ttl_seconds", float(ttl_seconds))
        # key -> (stored_at_monotonic, value)
        object.__setattr__(self, "_cache", {})
        # Per-key single-flight locks. When N coroutines request the same
        # key concurrently only the first one misses and fetches from the
        # wrapped provider; the rest await the lock and then observe the
        # freshly stored value (see :meth:`_lock_for`). A
        # :class:`weakref.WeakValueDictionary` is used deliberately: once
        # no coroutine is holding/awaiting a lock it is garbage-collected,
        # so the mapping cannot grow unboundedly as distinct keys churn
        # through the cache over the process lifetime.
        object.__setattr__(
            self,
            "_key_locks",
            weakref.WeakValueDictionary(),
        )

    # ------------------------------------------------------------------ #
    # read-only surface, mainly for tests / introspection
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> IDataProvider:
        """The wrapped underlying provider."""
        return self._provider

    @property
    def ttl_seconds(self) -> float:
        """Configured cache TTL, in seconds."""
        return self._ttl_seconds

    #: Backwards-compatible alias matching :class:`engine.providers.cached`.
    @property
    def ttl(self) -> float:
        """Alias for :attr:`ttl_seconds`."""
        return self._ttl_seconds

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
    # internal helpers
    # ------------------------------------------------------------------ #
    def _is_fresh(self, stored_at: float, now: float) -> bool:
        # A TTL of 0 means "never serve from cache" — always re-fetch.
        if self._ttl_seconds == 0:
            return False
        return (now - stored_at) < self._ttl_seconds

    def _lookup(self, key: Any) -> Any:
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

    def _store(self, key: Any, value: Any) -> None:
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

    def _copy_result(self, value: Any) -> Any:
        """Return a defensive deep copy of a cached value when possible.

        Cached DataFrames are never handed back by reference: a caller that
        mutated the returned frame in place would silently corrupt the
        cached copy (and every subsequent reader). Scalars (``float``,
        ``None``) and objects without a ``copy`` method are returned
        unchanged.
        """
        copy = getattr(value, "copy", None)
        if not callable(copy):
            return value
        try:
            return copy(deep=True)
        except TypeError:
            # ``copy`` exists but doesn't take ``deep=`` (e.g.
            # ``list.copy``) — fall back to its default arity, and if even
            # that fails just return the original.
            try:
                return copy()
            except TypeError:
                return value

    @staticmethod
    def _stable_key(*parts: Any) -> tuple[Any, ...]:
        """Build a hashable cache key from arbitrary, possibly-unhashable parts.

        ``tuple(sorted(kwargs.items()))`` blows up with a ``TypeError`` the
        moment any argument is a ``list`` / ``dict`` (common for filter
        specs passed to ``get_instruments``) — and even when it doesn't
        raise, the resulting tuple embeds those mutable values directly so
        it cannot be used as a ``dict`` key. We instead serialise the
        parts with :func:`json.dumps` using ``sort_keys=True`` (so
        equivalent kwargs hash identically regardless of insertion order)
        and ``default=str`` (so non-JSON objects still produce a stable
        string rather than raising). The JSON string *is* hashable, so the
        returned tuple can safely index ``_cache``.
        """
        return tuple(
            json.dumps(part, sort_keys=True, default=str) for part in parts
        )

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
    # cached IDataProvider surface
    # ------------------------------------------------------------------ #
    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return cached OHLCV bars or fetch them from the wrapped provider.

        Cache key: ``("get_ohlcv", symbol, period, interval)``. An empty
        DataFrame returned by the wrapped provider ("unknown symbol") is
        cached like any other value so we don't keep asking for it.
        """
        key: tuple[Any, ...] = ("get_ohlcv", symbol, period, interval)
        hit = self._lookup(key)
        if hit is not _MISS:
            logger.debug(
                "data_provider.cache.hit",
                method="get_ohlcv",
                symbol=symbol,
                period=period,
                interval=interval,
            )
            return hit

        logger.debug(
            "data_provider.cache.miss",
            method="get_ohlcv",
            symbol=symbol,
            period=period,
            interval=interval,
        )
        value = await self._provider.get_ohlcv(symbol, period=period, interval=interval)
        self._store(key, value)
        return value

    async def get_instruments(self, *args: Any, **kwargs: Any) -> Any:
        """Return cached instrument list or fetch from the wrapped provider.

        ``get_instruments`` is not part of the canonical
        :class:`IDataProvider` interface, but several concrete adapters
        expose it (e.g. a broker's tradable-symbol catalog). The wrapper
        caches it the same way as OHLCV so repeated enumeration of a slow
        instrument list (e.g. on dashboard load) doesn't re-hit the
        upstream.

        Cache key: ``("get_instruments", args, sorted(kwargs.items()))``.
        If the wrapped provider does not expose ``get_instruments`` a clear
        :class:`AttributeError` is raised rather than silently swallowed.
        """
        key: tuple[Any, ...] = (
            "get_instruments",
            args,
            tuple(sorted(kwargs.items())),
        )
        hit = self._lookup(key)
        if hit is not _MISS:
            logger.debug(
                "data_provider.cache.hit",
                method="get_instruments",
            )
            return hit

        logger.debug(
            "data_provider.cache.miss",
            method="get_instruments",
        )
        method = getattr(self._provider, "get_instruments", None)
        if method is None:
            raise AttributeError(
                f"{type(self._provider).__name__} does not expose get_instruments"
            )
        value = await method(*args, **kwargs)
        self._store(key, value)
        return value

    # ------------------------------------------------------------------ #
    # transparent delegation for everything else on the IDataProvider surface
    # ------------------------------------------------------------------ #
    def __getattr__(self, name: str) -> Any:
        """Forward any uncached attribute to the wrapped provider.

        Only invoked when normal attribute lookup fails — i.e. for methods
        like ``get_latest_price``, ``stream_prices``, ``health_check`` that
        are not cached here. ``_provider`` may not be set yet during
        ``__init__`` (e.g. while pickling or copying), so guard against
        that to avoid infinite recursion.
        """
        provider = self.__dict__.get("_provider")
        if provider is None:
            raise AttributeError(name)
        return getattr(provider, name)


__all__ = ["DEFAULT_TTL_SECONDS", "CachedDataProvider"]
