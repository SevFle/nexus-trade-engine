"""Tests for engine.app — application factory, auth registry, data provider bootstrap."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.rate_limit import (
    InMemoryBucketBackend,
    RateLimitConfig,
    RateLimitMiddleware,
    ValkeyBucketBackend,
)
from engine.app import (
    _build_auth_registry,
    _configure_data_providers,
    create_app,
)
from engine.config import settings
from engine.data.providers import ProviderRegistration, get_registry
from engine.data.providers.base import AssetClass


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
            registry.deregister(p.name)
        with patch.object(settings, "data_providers_config", ""):
            _configure_data_providers()
            providers = registry.list_providers()
            assert len(providers) >= 1

    def test_config_file_failure_is_logged_not_raised(self):
        registry = get_registry()
        for p in list(registry.list_providers()):
            registry.deregister(p.name)
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


class TestRateLimitBackendSelection:
    """Verify the ``_build_rate_limit_backend`` branches in ``create_app``.

    The helper is a nested function inside ``create_app``, so we exercise
    it by inspecting the ``backend`` kwarg passed to ``RateLimitMiddleware``
    in ``app.user_middleware``.
    """

    @staticmethod
    def _get_rate_limit_middleware(app: FastAPI):
        return next(
            (m for m in app.user_middleware if m.cls is RateLimitMiddleware),
            None,
        )

    def test_default_uses_in_memory_backend(self):
        """When ``rate_limit_valkey_enabled=False`` (default), the backend
        is an InMemoryBucketBackend regardless of app.state."""
        app = create_app()
        mw = self._get_rate_limit_middleware(app)
        assert mw is not None
        assert isinstance(mw.kwargs["backend"], InMemoryBucketBackend)

    def test_valkey_enabled_but_no_client_falls_back_to_in_memory(
        self, monkeypatch
    ):
        """When ``rate_limit_valkey_enabled=True`` but the lifespan has not
        yet set ``app.state.valkey`` (the case in unit tests), the backend
        falls back to InMemoryBucketBackend and a warning is emitted."""
        from structlog.testing import capture_logs

        monkeypatch.setattr(
            "engine.app.settings.rate_limit_valkey_enabled", True
        )
        # app.state.valkey is NOT set — lifespan hasn't run.
        with capture_logs() as cap_logs:
            app = create_app()
        mw = self._get_rate_limit_middleware(app)
        assert mw is not None
        assert isinstance(mw.kwargs["backend"], InMemoryBucketBackend)
        # The fallback path emits a structlog warning event.
        assert any(
            entry.get("event") == "rate_limit.valkey_enabled_but_no_client"
            and entry.get("log_level") == "warning"
            for entry in cap_logs
        )

    def test_valkey_enabled_with_client_uses_valkey_backend(self, monkeypatch):
        """When ``rate_limit_valkey_enabled=True`` and a Valkey client is
        pre-bound to ``app.state``, the backend is ValkeyBucketBackend.

        We patch ``FastAPI.__init__`` (the same technique the suite-level
        conftest uses to inject auth overrides) so the freshly-constructed
        app has ``state.valkey`` set before ``_build_rate_limit_backend``
        is invoked.
        """
        monkeypatch.setattr(
            "engine.app.settings.rate_limit_valkey_enabled", True
        )

        fake_valkey = MagicMock(name="fake_valkey")
        original_init = FastAPI.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.state.valkey = fake_valkey

        monkeypatch.setattr(FastAPI, "__init__", patched_init)
        app = create_app()
        mw = self._get_rate_limit_middleware(app)
        assert mw is not None
        backend = mw.kwargs["backend"]
        assert isinstance(backend, ValkeyBucketBackend)
        # The backend was wired with the injected client.
        assert backend._client is fake_valkey

    def test_valkey_backend_uses_configured_key_ttl(self, monkeypatch):
        """The ``rate_limit_valkey_key_ttl_sec`` setting propagates."""
        monkeypatch.setattr(
            "engine.app.settings.rate_limit_valkey_enabled", True
        )
        monkeypatch.setattr(
            "engine.app.settings.rate_limit_valkey_key_ttl_sec", 7200
        )

        original_init = FastAPI.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.state.valkey = MagicMock(name="valkey")

        monkeypatch.setattr(FastAPI, "__init__", patched_init)
        app = create_app()
        mw = self._get_rate_limit_middleware(app)
        backend: ValkeyBucketBackend = mw.kwargs["backend"]
        assert backend._key_ttl_sec == 7200

    def test_rate_limit_config_carries_role_tiers(self, monkeypatch):
        monkeypatch.setattr(
            "engine.app.settings.rate_limit_role_tiers",
            '{"admin": [6000, 100]}',
        )
        app = create_app()
        mw = self._get_rate_limit_middleware(app)
        config: RateLimitConfig = mw.kwargs["config"]
        assert config.role_tiers == {"admin": (6000, 100)}

    def test_rate_limit_config_carries_route_overrides(self):
        app = create_app()
        mw = self._get_rate_limit_middleware(app)
        config: RateLimitConfig = mw.kwargs["config"]
        # The client-error route is always pinned to a tight cap.
        assert "/api/v1/client/errors" in config.overrides
        assert config.overrides["/api/v1/client/errors"] == (30, 30)
