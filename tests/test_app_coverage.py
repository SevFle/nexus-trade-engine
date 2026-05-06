"""Tests for engine.app — application factory, auth registry, data provider bootstrap."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.app import (
    _build_auth_registry,
    _configure_data_providers,
    create_app,
)
from engine.api.auth.dependency import get_current_user
from engine.config import settings
from engine.data.providers import ProviderRegistration, get_registry
from engine.data.providers.base import AssetClass
from tests.conftest import _fake_authenticated_user


class TestCreateApp:
    def test_create_app_returns_fastapi(self):
        app = create_app()
        assert isinstance(app, FastAPI)
        assert app.title == settings.app_name

    def test_create_app_includes_api_router(self):
        app = create_app()
        routes = [r.path for r in app.routes]
        assert any("/health" in r for r in routes)

    def test_create_app_has_middleware_stack(self):
        app = create_app()
        assert len(app.user_middleware) > 0


class TestBuildAuthRegistry:
    def test_local_provider_registered(self):
        with patch.object(settings, "auth_providers", "local"):
            registry = _build_auth_registry()
            assert "local" in registry.providers

    def test_unknown_provider_skipped(self):
        with patch.object(settings, "auth_providers", "nonexistent"):
            registry = _build_auth_registry()
            assert "nonexistent" not in registry.providers

    def test_oidc_provider_registered(self):
        with patch.object(settings, "auth_providers", "oidc"):
            registry = _build_auth_registry()
            assert "oidc" in registry.providers

    def test_empty_providers(self):
        with patch.object(settings, "auth_providers", ""):
            registry = _build_auth_registry()
            assert len(registry.providers) == 0

    def test_multiple_providers(self):
        with patch.object(settings, "auth_providers", "local,oidc"):
            registry = _build_auth_registry()
            assert "local" in registry.providers
            assert "oidc" in registry.providers


class TestConfigureDataProviders:
    def test_skip_if_already_configured(self):
        registry = get_registry()
        mock_provider = MagicMock()
        mock_provider.name = "mock_test"
        registry.register(
            ProviderRegistration(
                provider=mock_provider,
                priority=1,
                asset_classes=frozenset({AssetClass.EQUITY}),
            )
        )
        initial_count = len(registry.list_providers())
        _configure_data_providers()
        assert len(registry.list_providers()) == initial_count

    def test_default_yahoo_registered_when_no_config(self):
        registry = get_registry()
        for p in list(registry.list_providers()):
            registry.unregister(p.name)
        with patch.object(settings, "data_providers_config", ""):
            _configure_data_providers()
            providers = registry.list_providers()
            assert len(providers) >= 1

    def test_config_file_failure_is_logged_not_raised(self):
        registry = get_registry()
        for p in list(registry.list_providers()):
            registry.unregister(p.name)
        with (
            patch.object(settings, "data_providers_config", "/nonexistent/path.yaml"),
            patch("engine.app.configure_from_file", side_effect=FileNotFoundError("nope")),
        ):
            _configure_data_providers()


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_ok(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
