"""Tests for engine.app — application factory, auth registry, data provider bootstrap."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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

    async def test_create_app_includes_api_router(self):
        # Assert the API router is mounted by exercising a known route rather
        # than introspecting ``app.routes`` — newer Starlette wraps included
        # routers in an internal ``_IncludedRouter`` object whose paths are not
        # exposed flatly, so a structural check is brittle across versions. A
        # live request verifies the real behavior the test cares about.
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health")
        assert resp.status_code == 200

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

    def test_valkey_enabled_but_no_client_falls_back_to_in_memory(self, monkeypatch):
        """When ``rate_limit_valkey_enabled=True`` but the lifespan has not
        yet set ``app.state.valkey`` (the case in unit tests), the backend
        falls back to InMemoryBucketBackend and a warning is emitted."""
        from structlog.testing import capture_logs

        monkeypatch.setattr("engine.app.settings.rate_limit_valkey_enabled", True)
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
        monkeypatch.setattr("engine.app.settings.rate_limit_valkey_enabled", True)

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
        monkeypatch.setattr("engine.app.settings.rate_limit_valkey_enabled", True)
        monkeypatch.setattr("engine.app.settings.rate_limit_valkey_key_ttl_sec", 7200)

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


class TestShutdownGuaranteesCloseSentry:
    """The ``_shutdown`` helper must run every cleanup step with its own
    ``try/except`` guard and always call ``close_sentry`` at the end,
    even when a preceding step raises.
    """

    @pytest.mark.asyncio
    async def test_close_sentry_called_on_clean_shutdown(self):
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock()
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry") as mock_close,
        ):
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_sentry_called_even_when_step_raises(self):
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_bridge.stop.side_effect = RuntimeError("boom")
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock()
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry") as mock_close,
        ):
            # Should NOT raise — the exception is caught per-step.
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

        # Despite the exception, close_sentry must still have been called.
        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_steps_run_even_when_first_raises(self):
        """A failure in one step must not skip subsequent steps."""
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_bridge.stop.side_effect = RuntimeError("bridge down")
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock()
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()) as mock_dispose,
            patch("engine.app.close_sentry"),
        ):
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

        ws_manager.close_all.assert_awaited_once()
        event_bus.disconnect.assert_awaited_once()
        app.state.valkey.aclose.assert_awaited_once()
        mock_dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_middle_step_failure_still_runs_rest(self):
        """A failure in a middle step must not skip remaining steps."""
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock(side_effect=RuntimeError("ws boom"))
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()) as mock_dispose,
            patch("engine.app.close_sentry") as mock_close,
        ):
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

        event_bus.disconnect.assert_awaited_once()
        app.state.valkey.aclose.assert_awaited_once()
        mock_dispose.assert_awaited_once()
        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_sentry_failure_does_not_propagate(self):
        """If close_sentry itself raises the error must be swallowed."""
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock()
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry", side_effect=RuntimeError("flush fail")),
        ):
            # Must NOT raise even though close_sentry blows up.
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

    @pytest.mark.asyncio
    async def test_all_steps_failed_close_sentry_still_called(self):
        """When every step raises, close_sentry must still execute."""
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_bridge.stop.side_effect = RuntimeError("a")
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock(side_effect=RuntimeError("b"))
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock(side_effect=RuntimeError("c"))
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock(side_effect=RuntimeError("d"))

        with (
            patch("engine.app.dispose_engine", new=AsyncMock(side_effect=RuntimeError("e"))),
            patch("engine.app.close_sentry") as mock_close,
        ):
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_ws_order_signal_bridge_stopped_when_provided(self):
        """When ``ws_order_signal_bridge`` is passed it must also be stopped."""
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_order_signal_bridge = MagicMock()
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock()
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry"),
        ):
            await _shutdown(
                app,
                ws_bridge,
                ws_manager,
                event_bus,
                ws_order_signal_bridge=ws_order_signal_bridge,
            )

        ws_order_signal_bridge.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_ws_order_signal_bridge_optional_defaults_to_none(self):
        """``_shutdown`` must accept calls without ``ws_order_signal_bridge``.

        This pins backward compatibility for the positional 4-arg form
        ``(app, ws_bridge, ws_manager, event_bus)`` used by older callers;
        it must not raise even though ``ws_order_signal_bridge`` is omitted.
        """
        from engine.app import _shutdown

        ws_bridge = MagicMock()
        ws_manager = MagicMock()
        ws_manager.close_all = AsyncMock()
        event_bus = MagicMock()
        event_bus.disconnect = AsyncMock()
        app = MagicMock()
        app.state.valkey.aclose = AsyncMock()

        with (
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry") as mock_close,
        ):
            # Must NOT raise even though ws_order_signal_bridge is omitted.
            await _shutdown(app, ws_bridge, ws_manager, event_bus)

        mock_close.assert_called_once()


class TestInitSentryGuard:
    """``init_sentry()`` in the lifespan startup must be wrapped in a
    ``try/except`` so that a failure during initialisation does not abort
    the entire application startup — it should log a warning instead.
    """

    @pytest.mark.asyncio
    async def test_init_sentry_failure_logs_warning_and_continues(self):
        """When init_sentry raises, a warning is logged and the lifespan
        proceeds to the next step (``set_metrics``)."""
        from engine.app import lifespan

        mock_valkey = MagicMock()
        mock_valkey.aclose = AsyncMock()

        mock_app = MagicMock()
        mock_app.state.valkey = mock_valkey

        mock_ws_manager = MagicMock()
        mock_ws_manager.close_all = AsyncMock()

        mock_event_bus = MagicMock()
        mock_event_bus.disconnect = AsyncMock()
        mock_event_bus.connect = AsyncMock()

        mock_ws_bridge = MagicMock()
        mock_ws_bridge.stop = MagicMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        with (
            patch("engine.app.setup_logging"),
            patch("engine.app.setup_tracing"),
            patch("engine.app.init_sentry", side_effect=RuntimeError("dsn bad")),
            patch("engine.app.set_metrics") as mock_set_metrics,
            patch("engine.app.Valkey.from_url", return_value=mock_valkey),
            patch("engine.app._build_auth_registry"),
            patch("engine.app._configure_data_providers"),
            patch("engine.app._seed_reference_index"),
            patch("engine.app.get_session_factory", return_value=mock_session_factory),
            patch("engine.app.sync_legal_documents", new=AsyncMock(return_value=0)),
            patch("engine.app.ConnectionManager", return_value=mock_ws_manager),
            patch("engine.app.AuthRateLimiter"),
            patch("engine.app.init_ws"),
            patch("engine.events.bus.EventBus", return_value=mock_event_bus),
            patch("engine.app.EventBusBridge", return_value=mock_ws_bridge),
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry"),
        ):
            async with lifespan(mock_app):
                pass

        # set_metrics is called AFTER init_sentry — if the guard works,
        # this must have been called despite the RuntimeError.
        mock_set_metrics.assert_called_once()

    @pytest.mark.asyncio
    async def test_init_sentry_success_does_not_log_warning(self):
        """When init_sentry succeeds no warning is emitted."""
        from engine.app import lifespan

        mock_valkey = MagicMock()
        mock_valkey.aclose = AsyncMock()

        mock_app = MagicMock()
        mock_app.state.valkey = mock_valkey

        mock_ws_manager = MagicMock()
        mock_ws_manager.close_all = AsyncMock()

        mock_event_bus = MagicMock()
        mock_event_bus.disconnect = AsyncMock()
        mock_event_bus.connect = AsyncMock()

        mock_ws_bridge = MagicMock()
        mock_ws_bridge.stop = MagicMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        with (
            patch("engine.app.setup_logging"),
            patch("engine.app.setup_tracing"),
            patch("engine.app.init_sentry"),
            patch("engine.app.set_metrics"),
            patch("engine.app.Valkey.from_url", return_value=mock_valkey),
            patch("engine.app._build_auth_registry"),
            patch("engine.app._configure_data_providers"),
            patch("engine.app._seed_reference_index"),
            patch("engine.app.get_session_factory", return_value=mock_session_factory),
            patch("engine.app.sync_legal_documents", new=AsyncMock(return_value=0)),
            patch("engine.app.ConnectionManager", return_value=mock_ws_manager),
            patch("engine.app.AuthRateLimiter"),
            patch("engine.app.init_ws"),
            patch("engine.events.bus.EventBus", return_value=mock_event_bus),
            patch("engine.app.EventBusBridge", return_value=mock_ws_bridge),
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry"),
            patch("engine.app.logger") as mock_logger,
        ):
            async with lifespan(mock_app):
                pass

        # The warning about sentry setup failure should NOT have been logged.
        sentry_warning_calls = [
            c for c in mock_logger.warning.call_args_list if "sentry" in str(c).lower()
        ]
        assert sentry_warning_calls == []
