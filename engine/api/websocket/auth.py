"""JWT / API-key authentication for the WebSocket handshake (SEV-275).

The route handlers in :mod:`engine.api.websocket.handlers` call into
:func:`authenticate` after the underlying socket has been accepted.
The function tries every supported token-extraction method in turn
and returns a populated :class:`Principal` on success or raises one
of the :class:`WebSocketError` subclasses on failure.

Token extraction order
----------------------
1. ``Sec-WebSocket-Protocol`` subprotocol — preferred because the
   value is not logged by intermediaries the way query strings are.
   The server advertises ``nexus.<token>`` as a subprotocol on accept.
2. ``Authorization`` header — works when a reverse proxy terminates
   the upgrade and rewrites headers, but the browser WebSocket API
   cannot set arbitrary headers.
3. ``token`` query parameter — last-resort path used by browsers.
   Logged by intermediaries so callers should prefer short-lived
   tickets exchanged via a prior authenticated REST call.

After extraction we delegate to the existing JWT / API-key verifiers
so behaviour matches REST endpoints exactly.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from engine.api.auth.api_keys import find_active_by_token, is_engine_token
from engine.api.auth.jwt import decode_token
from engine.api.websocket.constants import CloseCode
from engine.api.websocket.exceptions import (
    AuthRequiredError,
    ForbiddenError,
    InvalidTokenError,
)
from engine.api.websocket.models import AuthMethod, Principal
from engine.db.models import User
from engine.db.session import get_session_factory

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = structlog.get_logger()


# Scope strings come straight from the API-key vocabulary. The
# ``portfolio``, ``orders``, ``market:read`` aliases below bridge into
# the same namespace so the same Principal.has_scope check works for
# both JWT- and API-key-authenticated connections.
_WS_SCOPES: dict[str, frozenset[str]] = {
    "user": frozenset({"portfolio:read", "orders:read", "market:read"}),
    "trader": frozenset(
        {"portfolio:read", "orders:read", "orders:write", "market:read"}
    ),
    "admin": frozenset({"admin"}),
}

# Channel family → required scope. Used by handlers when a client
# subscribes; the handshake itself only requires "any" valid scope.
CHANNEL_REQUIRED_SCOPE: dict[str, str] = {
    "portfolio": "portfolio:read",
    "orders": "orders:read",
    "market": "market:read",
    "market_depth": "market:read",
}


def _scopes_for_role(role: str, api_key_scopes: list[str] | None = None) -> frozenset[str]:
    """Resolve effective scopes from role + (optional) API-key scopes."""
    scopes: set[str] = set(_WS_SCOPES.get(role, _WS_SCOPES["user"]))
    if api_key_scopes:
        # API key scopes — `read` expands to all read scopes, `trade`
        # adds write scopes, `admin` implies everything.
        for s in api_key_scopes:
            if s == "read":
                scopes |= {"portfolio:read", "orders:read", "market:read"}
            elif s == "trade":
                scopes |= {"portfolio:read", "orders:read", "orders:write", "market:read"}
            elif s == "admin":
                scopes |= {"admin"}
    return frozenset(scopes)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------
def _extract_subprotocol(ws: WebSocket) -> str | None:
    """Pull a token out of the Sec-WebSocket-Protocol header.

    Convention: ``nexus.<token>`` — the leading ``nexus.`` is the
    routing prefix the server advertised at accept time. The leading
    and trailing whitespace is stripped defensively.
    """
    proto = ws.headers.get("sec-websocket-protocol") if hasattr(ws, "headers") else None
    if not proto:
        # Starlette exposes accepted subprotocols on query_params for
        # WebSocket — fall back to the scope directly.
        proto_list = ws.scope.get("subprotocols") if hasattr(ws, "scope") else None
        if proto_list:
            for candidate in proto_list:
                if isinstance(candidate, str) and candidate.startswith("nexus."):
                    proto = candidate
                    break
    if not proto:
        return None
    if not proto.startswith("nexus."):
        return None
    token = proto[len("nexus.") :].strip()
    return token or None


def _extract_authorization_header(ws: WebSocket) -> str | None:
    auth = ws.headers.get("authorization") if hasattr(ws, "headers") else None
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _extract_query_token(ws: WebSocket) -> str | None:
    token = ws.query_params.get("token") if hasattr(ws, "query_params") else None
    if not isinstance(token, str):
        return None
    token = token.strip()
    return token or None


def _extract_token(ws: WebSocket) -> tuple[str, AuthMethod] | None:
    """Return ``(token, method)`` for the first supported extraction
    method that yields a non-empty token, or ``None`` if all miss."""
    for extractor, method in (
        (_extract_subprotocol, "subprotocol"),
        (_extract_authorization_header, "header"),
        (_extract_query_token, "query"),
    ):
        token = extractor(ws)
        if token:
            return token, method  # type: ignore[return-value]
    return None


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------
async def _user_for_jwt(session, token: str) -> User | None:
    payload = decode_token(token)
    if payload is None:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        user_uuid = uuid.UUID(str(sub))
    except (ValueError, AttributeError):
        return None
    return await _load_active_user(session, user_uuid)


async def _user_for_api_key(session, token: str) -> tuple[User, list[str]] | None:
    row = await find_active_by_token(session, token)
    if row is None:
        return None
    user = await _load_active_user(session, row.user_id)
    if user is None:
        return None
    return user, list(row.scopes or [])


async def _load_active_user(session, user_uuid: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def authenticate(ws: WebSocket) -> Principal:
    """Authenticate the connection. Raises a :class:`WebSocketError`
    subclass on failure; the caller is responsible for emitting the
    close frame with the supplied ``code``.

    On success returns a populated :class:`Principal`. The caller
    should treat this as the source of truth for the connection's
    identity; the original token is *not* retained.
    """
    extracted = _extract_token(ws)
    if extracted is None:
        raise AuthRequiredError(reason="missing_token")
    token, method = extracted

    session_factory = get_session_factory()
    async with session_factory() as session:
        if is_engine_token(token):
            result = await _user_for_api_key(session, token)
            if result is None:
                raise InvalidTokenError(reason="invalid_token")
            user, api_key_scopes = result
            scopes = _scopes_for_role(user.role, api_key_scopes)
        else:
            user = await _user_for_jwt(session, token)
            if user is None:
                raise InvalidTokenError(reason="invalid_token")
            scopes = _scopes_for_role(user.role)

    # ``admin`` role overrides theForbiddenError check, but every
    # other role needs at least *some* read scope to use the WS API.
    if not scopes and user.role != "admin":
        raise ForbiddenError(reason="forbidden")

    return Principal(
        user_id=user.id,
        email=user.email,
        role=user.role,
        scopes=scopes,
        auth_method=method,
    )


def authorize_channel(principal: Principal, channel: str) -> None:
    """Raise :class:`ForbiddenError` if ``principal`` cannot subscribe
    to ``channel``. Called at subscribe time, not at handshake time,
    so a client can connect with a minimal scope set and later expand
    its subscriptions within its entitlements."""
    required = CHANNEL_REQUIRED_SCOPE.get(channel)
    if required is None:
        return  # unknown channels are filtered by the subscription registry
    if not principal.has_scope(required):
        raise ForbiddenError(reason="forbidden")


def close_code_for(exc: Exception) -> int:
    """Map an arbitrary exception to a WebSocket close code."""
    # Lazy import to avoid circular dep at module load.
    from engine.api.websocket.exceptions import WebSocketError

    if isinstance(exc, WebSocketError):
        return exc.code
    return CloseCode.INTERNAL_ERROR


__all__ = [
    "CHANNEL_REQUIRED_SCOPE",
    "authenticate",
    "authorize_channel",
    "close_code_for",
]
