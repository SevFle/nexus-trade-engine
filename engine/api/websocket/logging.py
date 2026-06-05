"""Correlation-aware structured logging for the WebSocket API (SEV-275).

Wraps :mod:`structlog` so every frame lifecycle log line carries the
connection's correlation id (derived from the JWT ``jti`` /
``correlation_id`` claim, or freshly minted at handshake) and the
user id. The bind is per-connection; the helper
:func:`bind_logger` returns a logger that needs no further
arguments on each call site.

Keeping this in its own module means handlers and the connection
manager don't have to thread a correlation id through every
function signature — it's bound on the bound logger.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from engine.api.websocket.models import Principal


def fresh_correlation_id() -> str:
    return uuid.uuid4().hex


def bind_logger(principal: Principal | None = None, *, connection_id: str | None = None) -> structlog.BoundLogger:
    """Return a structlog logger pre-bound with the connection's
    correlation id and principal. Safe to call with ``principal=None``
    for pre-auth logs (the handshake phase)."""
    values: dict[str, str] = {}
    if principal is not None:
        values["user_id"] = str(principal.user_id)
        if principal.correlation_id:
            values["correlation_id"] = principal.correlation_id
        values["auth_method"] = principal.auth_method
    if connection_id is not None:
        values["ws_conn"] = connection_id
    if "correlation_id" not in values:
        values["correlation_id"] = fresh_correlation_id()
    return structlog.get_logger().bind(**values)


__all__ = ["bind_logger", "fresh_correlation_id"]
