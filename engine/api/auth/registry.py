from __future__ import annotations

from typing import TYPE_CHECKING

from engine.api.auth.base import AuthResult

if TYPE_CHECKING:
    from engine.api.auth.base import IAuthProvider


class AuthProviderRegistry:
    def __init__(self) -> None:
        self._providers: list[IAuthProvider] = []

    def register(self, provider: IAuthProvider) -> None:
        self._providers.append(provider)

    def get(self, name: str) -> IAuthProvider | None:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    @property
    def providers(self) -> list[IAuthProvider]:
        return list(self._providers)

    async def authenticate(self, provider_name: str, **kwargs) -> AuthResult:
        provider = self.get(provider_name)
        if provider is None:
            return AuthResult(success=False, error=f"Unknown auth provider: {provider_name}")
        return await provider.authenticate(**kwargs)
