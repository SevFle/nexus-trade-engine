"""WebSocket authentication (SEV-275).

Token-bearing policy:

A token is taken from, in priority order:

1. ``Authorization: Bearer <token>`` header (preferred — mirrors REST auth).
2. ``Sec-WebSocket-Protocol`` subprotocol (``bearer.<token>`` or a bare
   token), usable from browsers that cannot set request headers on a WS
   handshake).
3. The ``token`` query parameter — a lowest-priority handshake fallback for
   clients that can set neither headers nor a subprotocol (e.g. some server
   SDKs). Because query strings are logged by proxies, load balancers and
   browser history, prefer the header or subprotocol path whenever possible.
4. The first JSON message (``{"type": "auth", "token": "..."}``) within
   ``auth_timeout`` seconds — retained for back-compat with existing clients.

``authenticate_websocket`` performs a lightweight decode + scope extraction.
``validate_session_token_for_ws`` mirrors the REST auth stack (decode → load
active user → scope check → legal acceptance) and is used when a DB session
is available (e.g. wired into the router). Per-IP rate limiting and token
refresh mid-session are supported.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from engine.api.auth.jwt import decode_token
from engine.api.ip_utils import is_trusted_proxy
from engine.api.ws.metrics import ws_metrics
from engine.api.ws.protocol import (
    WS_CLOSE_AUTH_FORBIDDEN,
    WS_CLOSE_AUTH_INVALID,
    WS_CLOSE_AUTH_TIMEOUT,
    WS_CLOSE_LEGAL_REACCEPT,
)
from engine.db.models import User
from engine.legal import service as legal_service

if TYPE_CHECKING:
    from fastapi import WebSocket
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_TRUSTED_PROXIES: frozenset[str] = frozenset(
    os.environ.get("TRUSTED_PROXIES", "").split(",") if os.environ.get("TRUSTED_PROXIES") else []
)

_BEARER_SCHEME = "bearer"
_SUBPROTOCOL_PREFIX = "bearer."
# ``Authorization`` splits into exactly a scheme and a token.
_EXPECTED_AUTH_HEADER_PARTS = 2


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


def extract_scopes(token_data: dict) -> list[str]:
    """Extract scopes from JWT claims.

    Maps JWT role to scopes. Only admin and portfolio_manager receive
    :all scopes. All other roles get base read scopes only.
    """
    role = token_data.get("role", "viewer")
    role_scopes: dict[str, list[str]] = {
        "admin": [
            "read:portfolio",
            "read:portfolio:all",
            "read:orders",
            "read:orders:all",
            "read:strategies",
            "read:strategies:all",
        ],
        "portfolio_manager": [
            "read:portfolio",
            "read:portfolio:all",
            "read:orders",
            "read:orders:all",
            "read:strategies",
            "read:strategies:all",
        ],
        "quant_dev": ["read:portfolio", "read:orders", "read:strategies"],
        "developer": ["read:portfolio", "read:orders", "read:strategies"],
        "retail_trader": ["read:portfolio", "read:orders", "read:strategies"],
        "user": ["read:portfolio", "read:orders", "read:strategies"],
        "viewer": ["read:portfolio", "read:orders", "read:strategies"],
    }
    return role_scopes.get(role, role_scopes["viewer"])


def _strip_bearer(header_value: str) -> str | None:
    """Return the credential from an ``Authorization`` header value.

    Accepts only the ``Bearer`` scheme (case-insensitive). Returns ``None``
    for malformed values or a non-Bearer scheme.
    """
    parts = header_value.split(None, 1)
    if len(parts) != _EXPECTED_AUTH_HEADER_PARTS:
        return None
    scheme, value = parts[0], parts[1]
    if scheme.lower() != _BEARER_SCHEME:
        return None
    value = value.strip()
    return value or None


def _extract_token_from_handshake(ws: WebSocket) -> str | None:
    """Read a bearer token from the WS handshake.

    Tokens are read from, in priority:

    1. ``Authorization: Bearer <token>`` header.
    2. ``Sec-WebSocket-Protocol`` subprotocol — conventionally
       ``bearer.<token>`` (a single bare subprotocol value is also
       accepted for non-browser clients).
    3. The ``token`` query parameter — a lowest-priority fallback for
       clients that cannot set headers or a subprotocol. The query string
       is logged by proxies, so the header / subprotocol paths are
       preferred whenever available.

    Returns the token string, or ``None`` when none is present (the caller
    may then fall back to first-message auth).
    """
    auth_header = ws.headers.get("authorization")
    if auth_header:
        token = _strip_bearer(auth_header)
        if token:
            return token

    subprotocol = ws.headers.get("sec-websocket-protocol")
    if subprotocol:
        candidates = [c.strip() for c in subprotocol.split(",") if c.strip()]
        for candidate in candidates:
            if candidate.lower().startswith(_SUBPROTOCOL_PREFIX):
                token = candidate[len(_SUBPROTOCOL_PREFIX) :].strip()
                if token:
                    return token
        # No bearer.-prefixed value; accept a single bare token. Multiple
        # comma-separated non-bearer values are ambiguous, so we refuse to
        # guess.
        if len(candidates) == 1:
            return candidates[0]

    # Lowest-priority fallback: the ``token`` query parameter. Kept for
    # clients that cannot set request headers or a subprotocol on the WS
    # handshake. Checked *after* the header and subprotocol paths so a
    # credential supplied there always wins.
    query_params = getattr(ws, "query_params", None)
    if query_params is not None:
        token = query_params.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


async def _receive_auth_token(ws: WebSocket, auth_timeout: float) -> str | tuple[int, str]:
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
    db: AsyncSession | None = None,
) -> AuthResult | tuple:
    """Authenticate a WebSocket connection.

    The token is read (in order) from the ``Authorization`` header, the
    ``Sec-WebSocket-Protocol`` subprotocol, the ``token`` query parameter
    (lowest priority), or the first JSON ``auth`` message within
    ``auth_timeout`` seconds — see :func:`_extract_token_from_handshake`.

    When a ``db`` session is supplied the full REST-mirroring validation
    (:func:`validate_session_token_for_ws`) runs, enforcing legal
    acceptance and account-revocation checks. Without one a lightweight
    decode + scope extraction is used.

    Returns ``AuthResult`` on success, or ``(close_code, reason)`` on failure.
    """
    remote_ip = _get_remote_ip(ws)

    if rate_limiter is not None:
        allowed = await rate_limiter.check(remote_ip)
        if not allowed:
            ws_metrics.metrics.counter("sev_ws_auth_failures_total", tags={"reason": "ratelimit"})
            logger.warning("ws.auth_rate_limited", remote_ip=remote_ip)
            return WS_CLOSE_AUTH_INVALID, "auth rate limited"

    token = _extract_token_from_handshake(ws)

    if token is None:
        result = await _receive_auth_token(ws, auth_timeout)
        if isinstance(result, tuple):
            return result
        token = result

    if db is not None:
        validation = await validate_session_token_for_ws(db, token)
        if not isinstance(validation, tuple):
            logger.info(
                "ws.authenticated",
                user_id=_hash_subject(validation.user_id),
                remote_ip=remote_ip,
            )
        return validation

    token_data = decode_token(token)
    if token_data is None:
        return WS_CLOSE_AUTH_INVALID, "invalid token"

    sub = token_data.get("sub")
    if not sub:
        return WS_CLOSE_AUTH_INVALID, "invalid token payload"

    scopes = extract_scopes(token_data)

    logger.info(
        "ws.authenticated",
        user_id=_hash_subject(sub),
        remote_ip=remote_ip,
    )

    return AuthResult(user_id=sub, scopes=scopes, token_data=token_data)


async def _load_active_user(db: AsyncSession, user_uuid: uuid.UUID) -> User | None:
    """Load a user, treating disabled / missing accounts as revoked."""
    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def _check_legal_acceptance(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[int, str] | None:
    """Return a ``(close_code, reason)`` rejection when legal docs block access.

    Otherwise returns ``None``. Fails closed if the legal-documents store is
    unavailable — consistent with REST, but degraded to a generic auth failure
    so the connection handler does not crash.
    """
    try:
        pending = await legal_service.get_pending_acceptances(db, user_id)
    except Exception:
        logger.exception("ws.legal_check_failed")
        return WS_CLOSE_AUTH_INVALID, "auth validation failed"
    if pending:
        ws_metrics.metrics.counter(
            "sev_ws_auth_failures_total", tags={"reason": "legal"}
        )
        return WS_CLOSE_LEGAL_REACCEPT, "legal re-acceptance required"
    return None


async def validate_session_token_for_ws(
    db: AsyncSession,
    token: str,
    *,
    required_scopes: list[str] | None = None,
    enforce_legal: bool = True,
) -> AuthResult | tuple[int, str]:
    """Validate a WS session token, mirroring the REST auth stack.

    This is the WS analogue of ``get_current_user`` + the
    ``require_legal_acceptance`` dependency:

    1. Decode the JWT (rejects wrong type / expired / bad signature).
    2. Resolve ``sub`` to a real, active user. A missing or disabled
       user is treated as a revoked session.
    3. Derive scopes from the role and, when ``required_scopes`` is
       supplied, verify the token grants *every* required scope.
    4. Apply legal-acceptance enforcement unless ``enforce_legal`` is
       ``False`` (mirrors HTTP 451 — pending documents block access).

    Returns an :class:`AuthResult` on success, or a
    ``(close_code, reason)`` tuple describing why the token was rejected.
    """
    token_data = decode_token(token)
    if token_data is None:
        return WS_CLOSE_AUTH_INVALID, "invalid token"

    sub = token_data.get("sub")
    try:
        user_uuid = uuid.UUID(str(sub)) if sub else None
    except (ValueError, AttributeError, TypeError):
        user_uuid = None
    if user_uuid is None:
        return WS_CLOSE_AUTH_INVALID, "invalid token payload"

    user = await _load_active_user(db, user_uuid)
    if user is None:
        ws_metrics.metrics.counter(
            "sev_ws_auth_failures_total", tags={"reason": "revoked"}
        )
        return WS_CLOSE_AUTH_INVALID, "user not found or revoked"

    scopes = extract_scopes(token_data)
    if required_scopes and not all(scope in scopes for scope in required_scopes):
        ws_metrics.metrics.counter(
            "sev_ws_auth_failures_total", tags={"reason": "scope"}
        )
        return WS_CLOSE_AUTH_FORBIDDEN, "insufficient scope"

    if enforce_legal:
        legal_error = await _check_legal_acceptance(db, user.id)
        if legal_error is not None:
            return legal_error

    logger.info("ws.session_validated", user_id=_hash_subject(sub))
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
    scopes = extract_scopes(token_data)
    return AuthResult(user_id=sub, scopes=scopes, token_data=token_data)


def _get_remote_ip(ws: WebSocket) -> str:
    # CIDR-aware check: a ``_TRUSTED_PROXIES`` entry like ``"10.0.0.0/8"`` must
    # match any peer in that range, not just a literal string match. Falls
    # through (returns ``False``) for an empty proxy set, so the guard below
    # doubles as the emptiness check the old ``if _TRUSTED_PROXIES`` did.
    if ws.client and is_trusted_proxy(ws.client.host, _TRUSTED_PROXIES):
        forwarded = ws.headers.get("x-forwarded-for")
        if forwarded:
            # ``rsplit`` with a maxsplit of 1 yields at most two elements, so a
            # pathologically long (or hostile) XFF header cannot force the
            # allocation of a multi-million-entry list just to read the
            # rightmost (proxy-appended) hop.
            ip_str = forwarded.rsplit(",", 1)[-1].strip()
            try:
                ipaddress.ip_address(ip_str)
            except ValueError:
                pass
            else:
                return ip_str
        real_ip = ws.headers.get("x-real-ip")
        if real_ip:
            ip_str = real_ip.strip()
            try:
                ipaddress.ip_address(ip_str)
            except ValueError:
                pass
            else:
                return ip_str
    if ws.client:
        return ws.client.host
    return "unknown"
