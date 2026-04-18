from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class UserInfo:
    external_id: str | None = None
    email: str = ""
    display_name: str = ""
    provider: str = "local"
    roles: list[str] = field(default_factory=lambda: ["user"])
    raw_claims: dict = field(default_factory=dict)


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
    async def authenticate(self, **kwargs) -> AuthResult: ...

    @abstractmethod
    async def get_user_info(self, external_id: str) -> UserInfo | None: ...

    async def create_user(self, user_info: UserInfo, password: str | None = None) -> AuthResult:  # noqa: ARG002
        return AuthResult(success=False, error="create_user not supported")

    def map_roles(self, external_roles: list[str]) -> str:  # noqa: ARG002
        return "user"
