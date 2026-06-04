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


# Aliases that translate external IdP role names (e.g. as expressed in OIDC
# claims or LDAP groupDNs) into the canonical internal role vocabulary. These
# are *aliases*, not promotions — applying them must not change the privilege
# level of an external role, only spell it the way the rest of the engine
# expects (``viewer`` is what the IdP calls a ``user``; ``quant_dev`` is what
# it calls a ``developer``). The mapping is applied **before** priority
# selection in :meth:`IAuthProvider.map_roles` so that mixed-role inputs
# behave monotonically: adding a role to the input set can never lower the
# resulting canonical role.
_EXTERNAL_ROLE_ALIASES: dict[str, str] = {
    "viewer": "user",
    "quant_dev": "developer",
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
        """Fold a list of external IdP role names into one canonical role.

        Only external identity providers should call this — internal role
        lookups (e.g. from a JWT ``sub``) must read :attr:`User.role` directly
        so that an alias mapping can never silently elevate a principal.
        """
        role_priority: dict[str, int] = {
            "viewer": 0,
            "user": 1,
            "retail_trader": 2,
            "quant_dev": 3,
            "developer": 4,
            "portfolio_manager": 5,
            "admin": 6,
        }
        best = "user"
        for role in external_roles:
            normalized = role.lower().strip()
            # Translate external alias → canonical internal name BEFORE
            # comparing priorities, so that adding roles is monotonic.
            canonical = _EXTERNAL_ROLE_ALIASES.get(normalized, normalized)
            if canonical in role_priority and role_priority[canonical] > role_priority[best]:
                best = canonical
        return best
