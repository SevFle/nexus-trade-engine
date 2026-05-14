"""AuthBackend protocol per ADR-0002.

Defines the pluggable authentication interface. Selected via
``NEXUS_AUTH_BACKEND`` env var.  The default JWT-on-Postgres
implementation lives in ``engine.api.auth.jwt``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid


@runtime_checkable
class AuthBackend(Protocol):
    async def authenticate(self, request: Any) -> dict[str, Any] | None:
        """Validate credentials from *request* and return identity claims or None."""
        ...

    async def issue_token(self, user_id: uuid.UUID, scopes: list[str]) -> str:
        """Issue an access token for *user_id* with the given *scopes*."""
        ...

    async def revoke_token(self, token_id: str) -> None:
        """Revoke a previously issued token."""
        ...
