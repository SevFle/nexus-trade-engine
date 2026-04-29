"""Shared HTTP plumbing for adapters.

Wraps :class:`httpx.AsyncClient` with the rate limiter, retry policy and
cache so each adapter only owns its endpoint logic and response parsing.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
import pandas as pd
import structlog

from engine.data.providers._cache import ProviderCache
from engine.data.providers._resilience import TokenBucket, call_with_retry
from engine.data.providers.base import (
    OHLCV_COLUMNS,
    SYMBOL_PATTERN,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    HealthStatus,
    TransientProviderError,
)

logger = structlog.get_logger()

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_OHLCV_TTL_S = 60
ERROR_BODY_PREVIEW = 200
RESPONSE_BYTE_CAP = 16 * 1024 * 1024  # 16 MB hard cap on a single response

HTTP_CLIENT_ERROR_MIN = 400
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 600
TRANSIENT_STATUS = frozenset({408, 425, 429})
AUTH_STATUS = frozenset({401, 403})

_SYMBOL_RE = re.compile(SYMBOL_PATTERN)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[-_]?key|apikey|secret|signature|token|authorization)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9_\-]{24,}\b"),  # bearer tokens / signatures
)
# Header names whose values must never be overridden by per-call headers.
LOCKED_AUTH_HEADERS: frozenset[str] = frozenset(
    h.lower()
    for h in (
        "authorization",
        "apca-api-key-id",
        "apca-api-secret-key",
        "x-mbx-apikey",
        "x-cg-api-key",
    )
)


def validate_symbol(symbol: str) -> str:
    """Reject symbols that could break out of a URL path or hit unintended hosts.

    Symbols are user-supplied and flow into ``f"/v2/aggs/ticker/{symbol}/..."``.
    Without validation an adversarial symbol like ``"http://attacker"`` would
    redirect httpx off our base_url and exfiltrate auth headers (SSRF).
    Path-traversal sequences like ``..`` are also rejected.
    """
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol) or ".." in symbol:
        raise FatalProviderError(f"invalid symbol: {symbol!r}")
    return symbol


def encode_path_segment(symbol: str) -> str:
    """URL-encode a validated symbol for safe interpolation into a path."""
    return quote(validate_symbol(symbol), safe="")


def redact_secrets(text: str) -> str:
    """Strip likely secret material from a body preview before logging it."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


async def _read_capped(
    response: httpx.Response,
    cap: int,
    *,
    strict: bool = False,
    provider: str = "provider",
) -> bytes:
    """Stream-read at most ``cap`` bytes from ``response``.

    With ``strict=False`` (error-preview mode) the read silently stops at
    ``cap`` and returns whatever was collected. With ``strict=True`` (success
    body) the read raises :class:`FatalProviderError` the moment the
    accumulated size would exceed ``cap`` — so we never buffer a hostile
    multi-GB body just to reject it after the fact.
    """
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        if len(buf) + len(chunk) > cap:
            if strict:
                raise FatalProviderError(
                    f"{provider} response too large: exceeded {cap} bytes"
                )
            buf.extend(chunk[: cap - len(buf)])
            break
        buf.extend(chunk)
    return bytes(buf)


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
        self._base_host = httpx.URL(self._base_url).host
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

    def _resolve_url(self, path: str) -> httpx.URL:
        """Resolve ``path`` against the base URL and refuse cross-host targets.

        Stops absolute URLs or off-base redirects from leaking auth headers.
        """
        target = httpx.URL(self._base_url).join(path)
        if target.host and target.host != self._base_host:
            raise FatalProviderError(
                f"{self.capability.name} refused cross-host path "
                f"(base={self._base_host}, target={target.host})"
            )
        return target

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        client = await self._ensure_client()
        # Default auth headers always win — caller-supplied headers can extend
        # but never overwrite our locked auth header set.
        if headers:
            for key in headers:
                if key.lower() in LOCKED_AUTH_HEADERS:
                    raise FatalProviderError(
                        f"{self.capability.name} refusing to override locked header: {key}"
                    )
        merged_headers = {**(headers or {}), **self._headers} or None
        target = self._resolve_url(path)

        async def call() -> Any:
            await self._bucket.acquire()
            try:
                async with client.stream(
                    method, target, params=params, json=json, headers=merged_headers
                ) as response:
                    status = response.status_code

                    # For error statuses, read up to the preview cap only —
                    # never buffer the full body of a hostile/malformed reply.
                    if (
                        status in TRANSIENT_STATUS
                        or HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX
                    ):
                        preview = redact_secrets(
                            (await _read_capped(response, ERROR_BODY_PREVIEW)).decode(
                                "utf-8", errors="replace"
                            )
                        )
                        raise TransientProviderError(
                            f"{self.capability.name} HTTP {status}: {preview}"
                        )
                    if status in AUTH_STATUS:
                        raise FatalProviderError(
                            f"{self.capability.name} auth error {status}"
                        )
                    if status >= HTTP_CLIENT_ERROR_MIN:
                        preview = redact_secrets(
                            (await _read_capped(response, ERROR_BODY_PREVIEW)).decode(
                                "utf-8", errors="replace"
                            )
                        )
                        raise FatalProviderError(
                            f"{self.capability.name} HTTP {status}: {preview}"
                        )

                    # 2xx: stream body up to RESPONSE_BYTE_CAP and abort hard
                    # if the cap is exceeded — without ever buffering past it.
                    body = await _read_capped(
                        response,
                        RESPONSE_BYTE_CAP,
                        strict=True,
                        provider=self.capability.name,
                    )
            except httpx.TimeoutException as exc:
                raise TransientProviderError(
                    f"{self.capability.name} timeout"
                ) from exc
            except httpx.RequestError as exc:
                raise TransientProviderError(
                    f"{self.capability.name} network: {type(exc).__name__}"
                ) from exc

            try:
                import json as _json

                return _json.loads(body)
            except ValueError as exc:
                raise FatalProviderError(
                    f"{self.capability.name} returned non-JSON"
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
                detail=type(exc).__name__,
            )
        except TransientProviderError as exc:
            return HealthCheckResult(
                name=self.capability.name,
                status=HealthStatus.DEGRADED,
                detail=type(exc).__name__,
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        return HealthCheckResult(
            name=self.capability.name,
            status=HealthStatus.UP,
            latency_ms=elapsed_ms,
        )
