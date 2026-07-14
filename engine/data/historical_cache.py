"""In-memory LRU cache for historical OHLCV data loads.

The *live* market-data path (:class:`engine.data.providers._cache.ProviderCache`)
is async and Valkey/Redis-backed because it deduplicates expensive remote HTTP
calls. The *historical* path (:mod:`engine.data.provider`) is synchronous and
file-backed (CSV/Parquet), so its cache is a small in-process LRU keyed by the
source file's identity *and* its on-disk fingerprint (resolved path +
``mtime_ns`` + size). A stale entry can never be served after the underlying
file changes, because a changed file produces a different fingerprint and
therefore a cache miss — there is no TTL to tune.

Design mirrors :class:`ProviderCache` deliberately:

* :meth:`HistoricalDataCache.make_key` derives a deterministic, namespaced key
  from the call parameters and rejects non-primitive options (so ``"1"`` and
  ``1`` can never collide) — exactly like ``ProviderCache.make_key``.
* :meth:`HistoricalDataCache.shared` / :meth:`reset_for_tests` give a
  process-wide singleton that unit tests can reset.
* A per-entry byte cap (:data:`CACHE_ENTRY_CAP`) bounds memory: oversized
  frames are logged and skipped rather than cached, mirroring the live cache's
  ``CACHE_PAYLOAD_CAP``.

Non-primitive read kwargs (e.g. polars ``schema_overrides``) cannot go straight
into :meth:`make_key`; fold them through :func:`fingerprint_kwargs` first.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import polars as pl

logger = structlog.get_logger()

#: Namespace prefix for every historical-cache key. Mirrors the live cache's
#: ``nexus:dp:v1:`` scheme so the two never collide even if they ever share a
#: backing store.
_KEY_PREFIX = "nexus:hdp:v1"

#: Cap a single cached frame at 64 MiB to bound memory. Historical OHLCV files
#: are usually far smaller; a pathological/huge file is logged and skipped.
CACHE_ENTRY_CAP = 64 * 1024 * 1024

#: Default LRU bound (number of distinct source/option frames retained).
DEFAULT_MAX_ENTRIES = 128


@dataclass(frozen=True)
class CacheStats:
    """Snapshot of cache counters for observability and tests."""

    hits: int
    misses: int
    evictions: int
    entries: int
    size_bytes: int


def fingerprint_kwargs(kwargs: Any) -> str:
    """Return a stable hex digest for arbitrary read ``**kwargs``.

    Historical loaders forward backend-specific options (e.g. polars
    ``schema_overrides``) that are *not* JSON-primitives, so they cannot go
    straight into :meth:`HistoricalDataCache.make_key` (which rejects
    non-primitives to avoid collisions). This helper canonicalises any
    mapping/sequence of objects to a deterministic ``repr`` and hashes it, so
    two calls with the same kwargs share a key while differing kwargs do not.
    """
    payload = repr(_canonical(kwargs))
    return hashlib.sha256(payload.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    """Return a deterministically-ordered representation of ``value``.

    Dicts become sorted ``[(key, canonical(value)), ...]`` lists so ``repr`` is
    stable regardless of insertion order; lists/tuples recurse. Anything else
    (including polars dtype objects, whose ``repr`` is stable) is returned
    verbatim.
    """
    if isinstance(value, dict):
        return sorted((str(k), _canonical(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


class HistoricalDataCache:
    """Synchronous in-memory LRU cache for historical OHLCV frames.

    The cache is intentionally process-local: historical loads are cheap file
    reads, so the win is avoiding repeated parse/normalisation work across
    backtest runs that re-read the same source. Entries are keyed by the source
    file's *fingerprint* (resolved path + ``mtime_ns`` + size) plus the caller's
    options, so editing the file invalidates it automatically — no TTL required.

    Thread-safe via a plain :class:`threading.Lock`; historical loads are
    synchronous and short, so contention is negligible.
    """

    _GLOBAL: HistoricalDataCache | None = None

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = CACHE_ENTRY_CAP,
    ) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        if max_entry_bytes <= 0:
            raise ValueError(f"max_entry_bytes must be positive, got {max_entry_bytes}")
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._store: OrderedDict[str, pl.DataFrame] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @classmethod
    def shared(cls) -> HistoricalDataCache:
        """Return the process-wide cache, creating a default one on first use."""
        if cls._GLOBAL is None:
            cls._GLOBAL = cls()
        return cls._GLOBAL

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the shared singleton so tests start from a clean cache."""
        cls._GLOBAL = None

    # -- key derivation --------------------------------------------------

    @staticmethod
    def make_key(provider: str, source: str | Path, **options: object) -> str:
        """Build a deterministic cache key for a historical load.

        The key folds in the resolved absolute path, the file's on-disk
        fingerprint (``mtime_ns`` + ``size``) when the source exists, and a
        hash of the caller's ``options``. Non-existent sources get a
        path-only fingerprint (they will miss and the loader's own validation
        will raise).

        ``options`` must be JSON-primitives (``str``/``int``/``float``/
        ``bool``/``None``/``list``/``tuple``); non-primitives raise
        :class:`TypeError` exactly like :meth:`ProviderCache.make_key` so a
        non-primitive option can never silently collide with another. Fold
        non-primitive read kwargs through :func:`fingerprint_kwargs` first.
        """
        for name, value in options.items():
            if value is None:
                continue
            if not isinstance(value, (str, int, float, bool, list, tuple)):
                raise TypeError(
                    f"cache key option {name!r} must be a primitive, "
                    f"got {type(value).__name__}"
                )
        fingerprint = _fingerprint_source(source)
        digest = hashlib.sha256(
            json.dumps(options, sort_keys=True, default=str).encode()
        ).hexdigest()
        return f"{_KEY_PREFIX}:{provider}:{fingerprint}:{digest}"

    # -- access ----------------------------------------------------------

    def get(self, key: str) -> pl.DataFrame | None:
        """Return a clone of the cached frame, or ``None`` on a miss.

        The cached frame is cloned so a caller can never mutate the stored
        copy (e.g. via in-place polars operations) and corrupt a later hit.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                logger.debug("data_provider.historical.cache.miss", key=key)
                return None
            self._store.move_to_end(key)
            self._hits += 1
            logger.debug("data_provider.historical.cache.hit", key=key)
            return entry.clone()

    def put(self, key: str, df: pl.DataFrame) -> None:
        """Store ``df`` under ``key``, evicting least-recently-used if full.

        Oversized frames (above ``max_entry_bytes``) are logged and skipped
        rather than cached, mirroring the live cache's payload cap. Empty
        frames are likewise skipped — there is nothing to reuse.
        """
        if df is None or df.height == 0:
            return
        size = int(df.estimated_size("b"))
        if size > self._max_entry_bytes:
            logger.warning(
                "data_provider.historical.cache.refuse_oversized",
                key=key,
                size=size,
                cap=self._max_entry_bytes,
            )
            return
        with self._lock:
            if key in self._store:
                self._sizes.pop(key, None)
            self._store[key] = df
            self._sizes[key] = size
            self._store.move_to_end(key)
            self._evict_locked()

    def invalidate(self, key: str) -> bool:
        """Drop ``key`` if present; return whether anything was removed."""
        with self._lock:
            if key not in self._store:
                return False
            self._sizes.pop(key, None)
            self._store.pop(key, None)
            return True

    def clear(self) -> None:
        """Drop every cached frame and reset the hit/miss/eviction counters."""
        with self._lock:
            self._store.clear()
            self._sizes.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def stats(self) -> CacheStats:
        """Return a point-in-time snapshot of the cache counters."""
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                entries=len(self._store),
                size_bytes=sum(self._sizes.values()),
            )

    # -- internals -------------------------------------------------------

    def _evict_locked(self) -> None:
        """Evict least-recently-used entries until within ``max_entries``."""
        while len(self._store) > self._max_entries:
            evicted_key, _ = self._store.popitem(last=False)
            self._sizes.pop(evicted_key, None)
            self._evictions += 1
            logger.debug(
                "data_provider.historical.cache.evicted", key=evicted_key
            )


def _fingerprint_source(source: str | Path) -> str:
    """Return ``"<abspath>:<mtime_ns>:<size>"`` for an existing file.

    Falls back to ``"<abspath>"`` for non-existent paths or non-file sources
    (e.g. URLs): there is no mtime to fingerprint, so the key keeps the path
    and the loader's own validation handles the missing file.
    """
    path = Path(source)
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - defensive for exotic paths
        resolved = path
    try:
        stat = resolved.stat()
    except OSError:
        return resolved.as_posix()
    return f"{resolved.as_posix()}:{stat.st_mtime_ns}:{stat.st_size}"


__all__ = [
    "CACHE_ENTRY_CAP",
    "DEFAULT_MAX_ENTRIES",
    "CacheStats",
    "HistoricalDataCache",
    "fingerprint_kwargs",
]
