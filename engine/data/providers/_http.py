"""Shared HTTP plumbing for adapters.

Wraps :class:`httpx.AsyncClient` with the rate limiter, retry policy and
cache so each adapter only owns its endpoint logic and response parsing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pandas as pd
import structlog

from engine.data.providers._cache import ProviderCache
from engine.data.providers._resilience import TokenBucket, call_with_retry
from engine.data.providers.base import (
    OHLCV_COLUMNS,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    HealthStatus,
    TransientProviderError,
)

logger = structlog.get_logger()

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_OHLCV_TTL_S = 60

HTTP_CLIENT_ERROR_MIN = 400
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 600
TRANSIENT_STATUS = frozenset({408, 425, 429})
AUTH_STATUS = frozenset({401, 403})


def normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Project a raw bars DataFrame onto the canonical OHLCV layout.

    Drops rows with no close price, lower-cases columns, and ensures the
    result is indexed by an ascending UTC :class:`pandas.DatetimeIndex`.
    """
    if df.empty:
        return pd.DataFrame(columns=list(OHLCV_COLUMNS))

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise FatalProviderError(f"Provider response missing columns: {missing}")

    df = df.loc[:, list(OHLCV_COLUMNS)]
    df = df.dropna(subset=["close"])
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df = df.sort_index()
    return df


class HTTPProviderBase:
    """Mixin-style helper that adapters compose with rather than inherit blindly."""

    def __init__(
        self,
        capability: DataProviderCapability,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.capability = capability
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._headers = dict(default_headers or {})
        self._bucket = TokenBucket(capability.rate_limit)
        self._cache = cache or ProviderCache.shared()
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    headers=self._headers,
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        merged_headers = {**self._headers, **(headers or {})} or None

        async def call() -> dict[str, Any]:
            await self._bucket.acquire()
            try:
                response = await client.request(
                    method, path, params=params, json=json, headers=merged_headers
                )
            except httpx.TimeoutException as exc:
                raise TransientProviderError(f"{self.capability.name} timeout: {exc}") from exc
            except httpx.RequestError as exc:
                raise TransientProviderError(f"{self.capability.name} network: {exc}") from exc

            status = response.status_code
            if (
                status in TRANSIENT_STATUS
                or HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX
            ):
                raise TransientProviderError(
                    f"{self.capability.name} HTTP {status}: {response.text[:200]}"
                )
            if status in AUTH_STATUS:
                raise FatalProviderError(f"{self.capability.name} auth error {status}")
            if status >= HTTP_CLIENT_ERROR_MIN:
                raise FatalProviderError(
                    f"{self.capability.name} HTTP {status}: {response.text[:200]}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise FatalProviderError(
                    f"{self.capability.name} returned non-JSON: {exc}"
                ) from exc

        return await call_with_retry(call, provider=self.capability.name)

    async def _probe_health(self, path: str | None = None) -> HealthCheckResult:
        """Default liveness probe: ``GET <path>`` and report latency.

        Adapters expose a ``health_check()`` matching the
        :class:`IDataProvider` signature that delegates here.
        """
        if path is None:
            return HealthCheckResult(
                name=self.capability.name, status=HealthStatus.UP, detail="static"
            )

        started = time.monotonic()
        try:
            await self._request_json("GET", path)
        except FatalProviderError as exc:
            return HealthCheckResult(
                name=self.capability.name,
                status=HealthStatus.DOWN,
                detail=str(exc),
            )
        except TransientProviderError as exc:
            return HealthCheckResult(
                name=self.capability.name,
                status=HealthStatus.DEGRADED,
                detail=str(exc),
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        return HealthCheckResult(
            name=self.capability.name,
            status=HealthStatus.UP,
            latency_ms=elapsed_ms,
        )
