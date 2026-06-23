"""Authentication and authorization for the Nexus MCP server.

The MCP server reuses the engine's existing JWT validation
(:func:`engine.api.auth.jwt.decode_token`) and RBAC role hierarchy so that a
principal authenticated over MCP is indistinguishable from one authenticated
over the REST API.

Token transport
---------------
Because stdio MCP has no HTTP headers, the token is resolved in priority
order:

1. Per-request ``_meta.authorization`` (``Bearer <jwt>``) or ``_meta.api_key``
   — works for both transports.
2. The static API-key table (:attr:`MCPServerSettings.static_api_keys`).
3. The process-level :attr:`MCPServerSettings.token` (``NEXUS_MCP_TOKEN``) —
   the standard way to pass credentials to a local stdio server.

When ``auth_required`` is disabled (local dev) an anonymous principal with the
configured :attr:`default_role` is issued.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from engine.api.auth.dependency import ROLE_HIERARCHY
from engine.api.auth.jwt import decode_token
from engine.mcp.config import mcp_settings
from engine.mcp.errors import AuthenticationError, AuthorizationError

_BEARER_PREFIX = "bearer "


def _normalize_meta(meta: Any) -> Mapping[str, Any]:
    """Coerce an MCP ``RequestParams.Meta`` (or dict/None) to a flat mapping.

    MCP clients pass arbitrary keys in the request ``_meta``; the SDK stores
    them under ``model_extra``. We flatten that so downstream code can treat
    meta uniformly as a plain dict.
    """
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        return meta
    extra = getattr(meta, "model_extra", None) or {}
    out: dict[str, Any] = dict(extra)
    progress_token = getattr(meta, "progressToken", None)
    if progress_token is not None:
        out.setdefault("progressToken", progress_token)
    return out


@dataclass(frozen=True)
class AuthPrincipal:
    """The authenticated identity behind an MCP request."""

    user_id: str
    role: str
    email: str | None = None
    scopes: tuple[str, ...] = field(default=())
    auth_method: str = "jwt"  # jwt | api_key | anonymous

    @classmethod
    def anonymous(cls, role: str) -> AuthPrincipal:
        return cls(
            user_id="anonymous",
            role=role,
            email=None,
            auth_method="anonymous",
        )

    @property
    def role_level(self) -> int:
        return ROLE_HIERARCHY.get(self.role, 0)

    def has_role(self, minimum_role: str) -> bool:
        """True if this principal meets or exceeds ``minimum_role``."""
        return self.role_level >= ROLE_HIERARCHY.get(minimum_role, 0)

    def to_public_dict(self) -> dict[str, Any]:
        """A safe, loggable summary (never includes the raw token)."""
        return {
            "user_id": self.user_id,
            "role": self.role,
            "email": self.email,
            "auth_method": self.auth_method,
        }


def _extract_token(meta: Mapping[str, Any] | None) -> str | None:
    """Resolve a credential from MCP request metadata, then env fallbacks."""
    if meta:
        authz = meta.get("authorization") or meta.get("Authorization")
        if isinstance(authz, str) and authz:
            if authz.lower().startswith(_BEARER_PREFIX):
                return authz[len(_BEARER_PREFIX) :].strip() or None
            return authz.strip() or None
        api_key = meta.get("api_key") or meta.get("x-api-key")
        if isinstance(api_key, str) and api_key:
            return api_key.strip() or None

    # Process-level fallback (stdio local deployments).
    env_token = (mcp_settings.token or "").strip()
    return env_token or None


def extract_principal(meta: Any = None) -> AuthPrincipal:
    """Build an :class:`AuthPrincipal` from MCP request metadata.

    ``meta`` may be a plain mapping, an MCP ``RequestParams.Meta`` instance,
    or ``None``. Raises :class:`AuthenticationError` when auth is required but
    no valid credential is present.
    """
    meta_mapping = _normalize_meta(meta)

    if not mcp_settings.auth_required:
        return AuthPrincipal.anonymous(mcp_settings.default_role)

    token = _extract_token(meta_mapping)
    if not token:
        raise AuthenticationError("Authentication required: no credential provided")

    # 1. Engine JWT (reuses the exact validator the REST API uses).
    payload = decode_token(token)
    if payload is not None:
        return AuthPrincipal(
            user_id=str(payload.get("sub", "")),
            email=payload.get("email"),
            role=str(payload.get("role", "viewer")),
            auth_method="jwt",
        )

    # 2. Static API-key table (DB-free service tokens).
    mapped_role = mcp_settings.static_api_keys_map.get(token)
    if mapped_role is not None:
        return AuthPrincipal(
            user_id="api-key",
            role=mapped_role,
            auth_method="api_key",
        )

    raise AuthenticationError("Invalid or expired token")


def require_role(principal: AuthPrincipal, minimum_role: str) -> None:
    """Raise :class:`AuthorizationError` if ``principal`` is too low-trust."""
    if not principal.has_role(minimum_role):
        raise AuthorizationError(
            f"Permission denied: requires {minimum_role!r} role or higher",
            data={"required_role": minimum_role, "actual_role": principal.role},
        )


__all__ = ["AuthPrincipal", "extract_principal", "require_role"]
