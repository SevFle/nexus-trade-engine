"""WebSocket authentication (SEV-275).

Dual-mode JWT auth: query param or first-message within timeout.
Supports token refresh mid-session. Per-IP rate limiting.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from fastapi import WebSocket

from engine.api.auth.jwt import decode_token
from engine.api.ws.metrics import ws_metrics
from engine.api.ws.protocol import WS_CLOSE_AUTH_INVALID, WS_CLOSE_AUTH_TIMEOUT

logger = structlog.get_logger()

_TRUSTED_PROXIES: frozenset[str] = frozenset(
    os.environ.get("TRUSTED_PROXIES", "").split(",") if os.environ.get("TRUSTED_PROXIES") else []
)


@dataclass
class AuthResult:
    user_id: str
    scopes: list[str]
    token_data: dict[str, Any]


@dataclass
class _RateBucket:
    tokens: float
    last_refill: float


class AuthRateLimiter:
    """Token-bucket rate limiter per IP for auth attempts."""

    def __init__(self, max_attempts: int = 10, window_seconds: float = 60.0) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._buckets: dict[str, _RateBucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, ip: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.get(
                ip, _RateBucket(tokens=float(self._max_attempts), last_refill=now)
            )
            elapsed = now - bucket.last_refill
            refill = min(
                self._max_attempts,
                bucket.tokens + (elapsed / self._window) * self._max_attempts,
            )
            bucket.tokens = refill
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                self._buckets[ip] = bucket
                return True
            self._buckets[ip] = bucket
            return False


def _hash_subject(sub: str) -> str:
    return hashlib.sha256(sub.encode()).hexdigest()[:16]


def _extract_scopes(token_data: dict) -> list[str]:
    """Extract scopes from JWT claims.

    Maps JWT role to scopes. Only admin and portfolio_manager receive
    :all scopes. All other roles get base read scopes only.
    """
    role = token_data.get("role", "viewer")
    role_scopes: dict[str, list[str]] = {
        "admin": [
            "read:portfolio", "read:portfolio:all",
            "read:orders", "read:orders:all",
            "read:strategies", "read:strategies:all",
        ],
        "portfolio_manager": [
            "read:portfolio", "read:portfolio:all",
            "read:orders", "read:orders:all",
            "read:strategies", "read:strategies:all",
        ],
        "quant_dev": ["read:portfolio", "read:orders", "read:strategies"],
        "developer": ["read:portfolio", "read:orders", "read:strategies"],
        "retail_trader": ["read:portfolio", "read:orders", "read:strategies"],
        "user": ["read:portfolio", "read:orders", "read:strategies"],
        "viewer": ["read:portfolio", "read:orders", "read:strategies"],
    }
    return role_scopes.get(role, role_scopes["viewer"])


async def _receive_auth_token(
    ws: WebSocket, auth_timeout: float
) -> str | tuple[int, str]:
    try:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=auth_timeout)
    except TimeoutError:
        return WS_CLOSE_AUTH_TIMEOUT, "auth timeout"
    except Exception:
        return WS_CLOSE_AUTH_INVALID, "invalid auth message"
    if not isinstance(msg, dict):
        return WS_CLOSE_AUTH_INVALID, "invalid auth message"
    if msg.get("type") != "auth":
        return WS_CLOSE_AUTH_INVALID, "expected auth message"
    token = msg.get("token")
    if not isinstance(token, str) or not token:
        return WS_CLOSE_AUTH_INVALID, "missing token"
    return token


async def authenticate_websocket(
    ws: WebSocket,
    auth_timeout: float = 10.0,
    rate_limiter: AuthRateLimiter | None = None,
) -> AuthResult | tuple:
    """Authenticate a WebSocket connection.

    Accepts token from:
    1. Query param 'token'
    2. First JSON message within auth_timeout seconds

    Returns AuthResult on success, or (close_code, reason) on failure.
    """
    remote_ip = _get_remote_ip(ws)

    if rate_limiter is not None:
        allowed = await rate_limiter.check(remote_ip)
        if not allowed:
            ws_metrics.metrics.counter(
                "sev_ws_auth_failures_total", tags={"reason": "ratelimit"}
            )
            logger.warning("ws.auth_rate_limited", remote_ip=remote_ip)
            return WS_CLOSE_AUTH_INVALID, "auth rate limited"

    token = ws.query_params.get("token")

    if token is None:
        result = await _receive_auth_token(ws, auth_timeout)
        if isinstance(result, tuple):
            return result
        token = result

    token_data = decode_token(token)
    if token_data is None:
        return WS_CLOSE_AUTH_INVALID, "invalid token"

    sub = token_data.get("sub")
    if not sub:
        return WS_CLOSE_AUTH_INVALID, "invalid token payload"

    scopes = _extract_scopes(token_data)

    logger.info(
        "ws.authenticated",
        user_id=_hash_subject(sub),
        remote_ip=remote_ip,
    )

    return AuthResult(user_id=sub, scopes=scopes, token_data=token_data)


def validate_refresh_token(token) -> AuthResult | None:
    """Validate a refresh token. Returns None on failure."""
    if not isinstance(token, str):
        return None
    token_data = decode_token(token)
    if token_data is None:
        return None
    sub = token_data.get("sub")
    if not sub:
        return None
    scopes = _extract_scopes(token_data)
    return AuthResult(user_id=sub, scopes=scopes, token_data=token_data)


def _get_remote_ip(ws: WebSocket) -> str:
    if _TRUSTED_PROXIES and ws.client and ws.client.host in _TRUSTED_PROXIES:
        forwarded = ws.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = ws.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    if ws.client:
        return ws.client.host
    return "unknown"
