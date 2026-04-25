from __future__ import annotations

import structlog

from engine.api.auth.base import AuthResult, IAuthProvider

logger = structlog.get_logger()


class AuthProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, IAuthProvider] = {}
        self._order: list[str] = []

    def register(self, provider: IAuthProvider) -> None:
        name = provider.name
        self._providers[name] = provider
        if name not in self._order:
            self._order.append(name)
        logger.info("auth.provider_registered", provider=name)

    def get(self, name: str) -> IAuthProvider | None:
        return self._providers.get(name)

    @property
    def providers(self) -> dict[str, IAuthProvider]:
        return dict(self._providers)

    @property
    def ordered_names(self) -> list[str]:
        return list(self._order)

    async def authenticate(self, provider_name: str, **kwargs: object) -> AuthResult:
        provider = self._providers.get(provider_name)
        if provider is None:
            return AuthResult(success=False, error=f"Unknown provider: {provider_name}")
        return await provider.authenticate(**kwargs)
