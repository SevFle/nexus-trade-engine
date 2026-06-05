"""Typed value objects for the WebSocket API (SEV-275).

Principal
---------
Captures the authenticated identity bound to a connection after the
handshake succeeds. Kept deliberately small so it can be passed to
pure functions (subscription registries, channel resolvers) without
dragging in the full User ORM model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

AuthMethod = Literal["jwt", "api_key", "subprotocol", "query", "header"]


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated identity bound to a single WebSocket connection.

    ``scopes`` is the union of role-derived scopes and API-key scopes
    (when the token is an ``nxs_*`` key) — handlers check membership
    rather than roles so policy stays in one place.
    """

    user_id: uuid.UUID
    email: str
    role: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    auth_method: AuthMethod = "jwt"
    correlation_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        # ``admin`` implies every other scope; matches the API key model.
        return scope in self.scopes or "admin" in self.scopes


__all__ = ["AuthMethod", "Principal"]
