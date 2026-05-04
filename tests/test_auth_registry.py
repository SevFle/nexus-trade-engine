from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.api.auth.registry import AuthProviderRegistry


class FakeProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "fake"

    async def authenticate(self, **kwargs):
        return AuthResult(success=True, user_info=UserInfo(email="fake@test.com"))


class AnotherProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "another"

    async def authenticate(self, **kwargs):
        return AuthResult(success=True, user_info=UserInfo(email="another@test.com"))


class FailingProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "failing"

    async def authenticate(self, **kwargs):
        return AuthResult(success=False, error="intentional failure")


@pytest.fixture
def registry():
    return AuthProviderRegistry()


class TestRegistryRegister:
    def test_register_single_provider(self, registry):
        provider = FakeProvider()
        registry.register(provider)
        assert registry.get("fake") is provider

    def test_register_multiple_providers(self, registry):
        p1 = FakeProvider()
        p2 = AnotherProvider()
        registry.register(p1)
        registry.register(p2)
        assert registry.get("fake") is p1
        assert registry.get("another") is p2

    def test_register_same_provider_replaces(self, registry):
        p1 = FakeProvider()
        p2 = FakeProvider()
        registry.register(p1)
        registry.register(p2)
        assert registry.get("fake") is p2
        assert registry.ordered_names == ["fake"]


class TestRegistryGet:
    def test_get_returns_provider(self, registry):
        provider = FakeProvider()
        registry.register(provider)
        assert registry.get("fake") is provider

    def test_get_returns_none_for_unknown(self, registry):
        assert registry.get("nonexistent") is None

    def test_get_returns_none_when_empty(self):
        r = AuthProviderRegistry()
        assert r.get("anything") is None


class TestRegistryProviders:
    def test_providers_returns_copy(self, registry):
        provider = FakeProvider()
        registry.register(provider)
        providers = registry.providers
        assert "fake" in providers
        providers["new"] = provider
        assert "new" not in registry.providers

    def test_providers_empty_when_no_registrations(self):
        r = AuthProviderRegistry()
        assert r.providers == {}


class TestRegistryOrderedNames:
    def test_ordered_names_preserves_order(self, registry):
        registry.register(FakeProvider())
        registry.register(AnotherProvider())
        assert registry.ordered_names == ["fake", "another"]

    def test_ordered_names_returns_copy(self, registry):
        registry.register(FakeProvider())
        names = registry.ordered_names
        names.append("injected")
        assert registry.ordered_names == ["fake"]


class TestRegistryAuthenticate:
    async def test_authenticate_delegates_to_provider(self, registry):
        provider = FakeProvider()
        registry.register(provider)
        result = await registry.authenticate("fake")
        assert result.success is True
        assert result.user_info.email == "fake@test.com"

    async def test_authenticate_unknown_provider(self, registry):
        result = await registry.authenticate("unknown")
        assert result.success is False
        assert "Unknown provider" in result.error

    async def test_authenticate_passes_kwargs(self, registry):
        provider = FakeProvider()
        registry.register(provider)
        result = await registry.authenticate("fake", code="abc", db=None)
        assert result.success is True

    async def test_authenticate_failing_provider(self, registry):
        registry.register(FailingProvider())
        result = await registry.authenticate("failing")
        assert result.success is False
        assert "intentional failure" in result.error
