from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserInfo:
    external_id: str | None = None
    email: str = ""
    display_name: str = ""
    provider: str = "local"
    roles: list[str] = field(default_factory=lambda: ["user"])
    raw_claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthResult:
    success: bool = False
    user_info: UserInfo | None = None
    error: str | None = None


# External-to-internal role aliases.
#
# External identity providers (OIDC, LDAP, etc.) sometimes emit role names
# that overlap with names in our internal hierarchy but carry a different
# privilege intent. For example, Azure AD's default "viewer" group is closer
# to our internal ``user`` than to our (unused-but-reserved) ``viewer`` role.
# This map is applied *before* the priority comparison so the priority dict
# represents a pure internal-only hierarchy.
#
# Every value here MUST be a key in :data:`_ROLE_PRIORITY` and MUST have a
# strictly higher priority than its key (upward-only mapping). The
# ``test_external_aliases_only_promote_upward`` test enforces that invariant.
_EXTERNAL_ROLE_ALIASES: dict[str, str] = {
    "viewer": "user",
    "quant_dev": "developer",
}


# Internal role hierarchy. Lower number = lower privilege.
_ROLE_PRIORITY: dict[str, int] = {
    "viewer": 0,
    "user": 1,
    "retail_trader": 2,
    "quant_dev": 3,
    "developer": 4,
    "portfolio_manager": 5,
    "admin": 6,
}


class IAuthProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def authenticate(self, **kwargs: Any) -> AuthResult: ...

    async def get_user_info(self, _external_id: str) -> UserInfo | None:
        return None

    async def create_user(self, _user_info: UserInfo, **_kwargs: Any) -> AuthResult:
        return AuthResult(success=False, error=f"User creation not supported by {self.name}")

    def map_roles(self, external_roles: list[str]) -> str:
        """Reduce a list of IdP-supplied role names to a single internal role.

        Two-phase resolution keeps external naming concerns decoupled from
        the internal privilege hierarchy:

        1. **Alias resolution** — each input role is lowercased, stripped, and
           translated through :data:`_EXTERNAL_ROLE_ALIASES` to its canonical
           internal name. Unrecognized names are dropped.
        2. **Hierarchy selection** — the highest-priority canonical name wins.
           When the input contains no recognized role, ``"user"`` is returned
           as a safe default.
        """
        best = "user"
        best_priority = _ROLE_PRIORITY[best]
        for role in external_roles:
            normalized = role.lower().strip()
            canonical = _EXTERNAL_ROLE_ALIASES.get(normalized, normalized)
            priority = _ROLE_PRIORITY.get(canonical)
            if priority is None:
                continue
            if priority > best_priority:
                best = canonical
                best_priority = priority
        return best
