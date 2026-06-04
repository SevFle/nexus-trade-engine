from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


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
        """Map a list of external roles into a single canonical role.

        External claims are reflected faithfully: roles already present in the
        canonical hierarchy are honoured according to their priority; no role
        is silently elevated to a higher one. A warning is emitted for *every*
        unrecognized role so operators can spot mis-named groups or claims.
        When no claim matches a known role, ``"user"`` is returned as a safe
        baseline.
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
        best: str | None = None
        for role in external_roles:
            normalized = role.lower().strip()
            if normalized in role_priority:
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                logger.warning(
                    "auth.role.unknown",
                    role=normalized,
                    provider=self.name,
                )
        return best if best is not None else "user"
