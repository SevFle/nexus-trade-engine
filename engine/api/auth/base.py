from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog


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


_ROLE_PROMOTIONS: dict[str, str] = {
    "viewer": "user",
    "quant_dev": "developer",
}


logger = structlog.get_logger()


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
        recognized: list[str] = []
        unrecognized: list[str] = []
        for role in external_roles:
            normalized = role.lower().strip()
            if normalized in role_priority:
                recognized.append(normalized)
                if role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                unrecognized.append(role)
        if external_roles and not recognized:
            logger.warning(
                "auth.roles.unrecognized",
                provider=self.name,
                external_roles=list(external_roles),
                fallback_role=best,
                recognized_roles=sorted(role_priority.keys()),
            )
        return _ROLE_PROMOTIONS.get(best, best)
