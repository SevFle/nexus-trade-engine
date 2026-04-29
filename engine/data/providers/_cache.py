"""Redis/Valkey-backed response cache for data providers.

Keys are namespaced by provider + method + parameters so two adapters
can cache the same symbol without colliding. ``DataFrame`` payloads are
serialised via pandas ``to_json``/``read_json`` (orient="split") which
preserves dtypes and is faster than pickle for our shapes.

Falls back to an in-memory ``dict`` when Redis is not configured (tests,
local dev without a Valkey instance).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import time
from typing import Any, Protocol

import pandas as pd
import structlog

logger = structlog.get_logger()

# Cap an individual cached payload at 8 MB to bound memory + decode cost
# from a malicious or pathological provider response.
CACHE_PAYLOAD_CAP = 8 * 1024 * 1024


class _AsyncRedis(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ex: int | None = None) -> Any: ...
    async def ping(self) -> Any: ...
    async def aclose(self) -> Any: ...


class ProviderCache:
    """Thin async key-value cache used by every adapter.

    The constructor is intentionally lazy: a Redis connection is only opened
    on first use so unit tests that never call out can run without a server.
    """

    _GLOBAL: ProviderCache | None = None

    def __init__(self, url: str | None = None) -> None:
        self._url = url
        self._redis: _AsyncRedis | None = None
        self._memory: dict[str, tuple[float, bytes]] = {}
        self._lock = asyncio.Lock()
        self._enabled = bool(url)

    @classmethod
    def shared(cls) -> ProviderCache:
        if cls._GLOBAL is None:
            from engine.config import settings

            cls._GLOBAL = cls(settings.valkey_url)
        return cls._GLOBAL

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._GLOBAL = None

    async def _connect(self) -> _AsyncRedis | None:
        if not self._enabled:
            return None
        if self._redis is not None:
            return self._redis
        async with self._lock:
            if self._redis is not None:
                return self._redis
            try:
                import redis.asyncio as aioredis

                client: _AsyncRedis = aioredis.from_url(self._url or "")  # type: ignore[assignment]
                await client.ping()
                self._redis = client
            except Exception as exc:
                logger.warning("data_provider.cache.redis_unavailable", error=str(exc))
                self._enabled = False
                self._redis = None
        return self._redis

    @staticmethod
    def make_key(provider: str, method: str, **params: object) -> str:
        """Hash the call parameters into a deterministic cache key.

        Params must be JSON-serialisable primitives. Non-primitives are
        rejected up front rather than silently coerced via ``str``,
        which avoids cache collisions between e.g. ``"1"`` and ``1``.
        """
        for key, value in params.items():
            if value is None:
                continue
            if not isinstance(value, (str, int, float, bool, list, tuple)):
                raise TypeError(
                    f"cache key param {key!r} must be a primitive, got {type(value).__name__}"
                )
        payload = json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        return f"nexus:dp:v1:{provider}:{method}:{digest}"

    async def get_dataframe(self, key: str) -> pd.DataFrame | None:
        raw = await self._raw_get(key)
        if raw is None:
            return None
        if len(raw) > CACHE_PAYLOAD_CAP:
            logger.warning(
                "data_provider.cache.payload_too_large",
                key=key,
                size=len(raw),
            )
            return None
        try:
            df = pd.read_json(io.BytesIO(raw), orient="split")
        except ValueError as exc:
            logger.warning("data_provider.cache.decode_failed", key=key, error=str(exc))
            return None
        # ``read_json`` returns a tz-naive DatetimeIndex even when we serialised
        # an aware one — re-localise to UTC so callers can compare against
        # tz-aware timestamps without ``TypeError``.
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df

    async def set_dataframe(self, key: str, df: pd.DataFrame, ttl_seconds: int) -> None:
        if df is None or df.empty:
            return
        encoded = df.to_json(orient="split", date_format="iso")
        if encoded is None:
            return
        payload = encoded.encode()
        if len(payload) > CACHE_PAYLOAD_CAP:
            logger.warning(
                "data_provider.cache.refuse_oversized_set",
                key=key,
                size=len(payload),
            )
            return
        await self._raw_set(key, payload, ttl_seconds)

    async def get_json(self, key: str) -> object | None:
        raw = await self._raw_get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    async def set_json(self, key: str, value: object, ttl_seconds: int) -> None:
        payload = json.dumps(value, default=str).encode()
        await self._raw_set(key, payload, ttl_seconds)

    async def _raw_get(self, key: str) -> bytes | None:
        client = await self._connect()
        if client is not None:
            try:
                return await client.get(key)
            except Exception as exc:
                logger.warning("data_provider.cache.get_failed", key=key, error=str(exc))
                return None
        entry = self._memory.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at and expires_at < time.time():
            self._memory.pop(key, None)
            return None
        return value

    async def _raw_set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        client = await self._connect()
        if client is not None:
            try:
                await client.set(key, value, ex=ttl_seconds)
                return
            except Exception as exc:
                logger.warning("data_provider.cache.set_failed", key=key, error=str(exc))
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else 0.0
        self._memory[key] = (expires_at, value)

    async def aclose(self) -> None:
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
        self._redis = None
        self._memory.clear()
