from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from valkey.asyncio import Valkey

from engine.api.auth.local import LocalAuthProvider
from engine.api.auth.registry import AuthProviderRegistry
from engine.api.body_size_limit import BodySizeLimitMiddleware
from engine.api.rate_limit import (
    InMemoryBucketBackend,
    RateLimitConfig,
    RateLimitMiddleware,
    ValkeyBucketBackend,
)
from engine.api.router import api_router
from engine.api.routes.reference import get_search_index
from engine.api.security_headers import (
    SecurityHeadersConfig,
    SecurityHeadersMiddleware,
)
from engine.api.ws.auth import AuthRateLimiter
from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.event_bridge import EventBusBridge
from engine.api.ws.router import init_ws
from engine.config import settings
from engine.data.providers import (
    AssetClass,
    ProviderRegistration,
    YahooDataProvider,
    configure_from_file,
    get_registry,
)
from engine.db.session import dispose_engine, get_engine, get_session_factory
from engine.legal.sync import sync_legal_documents
from engine.observability.http_metrics import HttpMetricsMiddleware
from engine.observability.logging import setup_logging
from engine.observability.metrics import set_metrics
from engine.observability.middleware import CorrelationIdMiddleware
from engine.observability.prometheus import PrometheusBackend
from engine.observability.sentry import close_sentry, setup_sentry
from engine.observability.tracing import setup_tracing
from engine.reference.seed import seed_index

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from engine.events.bus import EventBus

logger = structlog.get_logger()


def _configure_data_providers() -> None:
    """Wire data providers into the registry on app start.

    Loads YAML at ``settings.data_providers_config`` if set; otherwise
    registers a keyless Yahoo adapter so the API works in dev without any
    further setup. Failures are logged but never abort startup — the API
    will still serve a 503 from the provider routes.
    """
    registry = get_registry()
    if registry.list_providers():
        return  # already configured (e.g. by tests)

    if settings.data_providers_config:
        try:
            configure_from_file(settings.data_providers_config, registry)
        except Exception:
            logger.exception("data_provider.bootstrap.failed", path=settings.data_providers_config)
        else:
            logger.info(
                "data_provider.bootstrap.from_file",
                path=settings.data_providers_config,
                count=len(registry.list_providers()),
            )
            return

    try:
        registry.register(
            ProviderRegistration(
                provider=YahooDataProvider(),
                priority=99,
                asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
            )
        )
        logger.info("data_provider.bootstrap.default", provider="yahoo")
    except ValueError:
        # Already registered by a parallel bootstrap; ignore.
        pass


def _build_auth_registry() -> AuthProviderRegistry:
    registry = AuthProviderRegistry()
    for provider_name in settings.enabled_providers:
        match provider_name:
            case "local":
                registry.register(LocalAuthProvider())
            case "google":
                from engine.api.auth.google import GoogleAuthProvider

                registry.register(GoogleAuthProvider())
            case "github":
                from engine.api.auth.github_oauth import GitHubAuthProvider

                registry.register(GitHubAuthProvider())
            case "oidc":
                from engine.api.auth.oidc import OIDCAuthProvider

                registry.register(OIDCAuthProvider())
            case "ldap":
                from engine.api.auth.ldap import LDAPAuthProvider

                registry.register(LDAPAuthProvider())
            case _:
                logger.warning("auth.unknown_provider", provider=provider_name)
    return registry


def _seed_reference_index() -> None:
    index = get_search_index()
    if index._records:
        return
    count = seed_index(index)
    logger.info("reference.seed.complete", instruments=count)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    # Install the global OTel TracerProvider and grab instrumentation
    # hooks for FastAPI + SQLAlchemy. The provider is a graceful no-op
    # when no OTLP collector is configured, so this never blocks startup.
    tracing_hooks = setup_tracing()
    try:
        tracing_hooks.instrument_fastapi(app)
    except Exception:
        logger.warning("nexus.tracing.fastapi_instrument_failed")
    try:
        # ``get_engine`` lazily creates the async engine; instrument it
        # before the first query runs (e.g. the legal-doc sync below).
        tracing_hooks.instrument_sqlalchemy(get_engine())
    except Exception:
        logger.warning("nexus.tracing.sqlalchemy_instrument_failed")
    try:
        setup_sentry()
    except Exception:
        logger.warning("nexus.sentry_setup_failed")
    # Switch the process-wide metrics singleton to a recording backend so
    # the /metrics route exposes real counters/gauges/histograms. Operators
    # who want a different exporter (OTel, StatsD, etc.) call set_metrics()
    # again after create_app() returns.
    set_metrics(PrometheusBackend())

    if not settings.is_test and not settings.secret_key:
        msg = "NEXUS_SECRET_KEY must be set outside the test environment"
        raise ValueError(msg)

    app.state.valkey = Valkey.from_url(settings.valkey_url)
    app.state.auth_registry = _build_auth_registry()
    logger.info("auth.providers_loaded", providers=list(app.state.auth_registry.providers.keys()))
    _configure_data_providers()
    _seed_reference_index()
    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            count = await sync_legal_documents(db)
            await db.commit()
        if count > 0:
            logger.info("legal.sync_complete", documents_synced=count)
    except Exception:
        logger.exception("legal.sync_failed")

    from engine.events.bus import EventBus

    ws_manager = ConnectionManager(
        max_connections=settings.ws_max_connections,
        send_queue_size=settings.ws_send_queue_size,
        max_subscriptions_per_connection=settings.ws_max_subscriptions_per_connection,
        heartbeat_interval=settings.ws_heartbeat_interval_seconds,
    )
    ws_rate_limiter = AuthRateLimiter(max_attempts=settings.ws_auth_rate_limit_per_minute)
    init_ws(ws_manager, rate_limiter=ws_rate_limiter)
    app.state.ws_manager = ws_manager

    event_bus = EventBus(redis_url=settings.valkey_url)
    await event_bus.connect()
    app.state.event_bus = event_bus

    ws_bridge = EventBusBridge(
        bus=event_bus,
        manager=ws_manager,
        concurrency=settings.ws_event_bridge_concurrency,
    )
    ws_bridge.start()
    app.state.ws_bridge = ws_bridge
    logger.info("ws.bridge_started")

    yield

    await _shutdown(app, ws_bridge, ws_manager, event_bus)


async def _shutdown(
    app: FastAPI,
    ws_bridge: EventBusBridge,
    ws_manager: ConnectionManager,
    event_bus: EventBus,
) -> None:
    """Run graceful teardown.

    Each cleanup step is wrapped in its own ``try/except`` so that a
    failure in one step does not prevent the remaining steps from
    executing.  :func:`close_sentry` runs last, inside its own guard, so
    the Sentry SDK flushes its event queue regardless of what happened
    above.
    """
    logger.info("nexus.shutdown")

    async def _stop_bridge() -> None:
        ws_bridge.stop()

    async def _close_websockets() -> None:
        await ws_manager.close_all(code=1000, reason="server_shutdown")

    async def _disconnect_bus() -> None:
        await event_bus.disconnect()

    async def _close_valkey() -> None:
        await app.state.valkey.aclose()

    async def _dispose_engine() -> None:
        await dispose_engine()

    cleanup_steps: list[tuple[str, Any]] = [
        ("ws_bridge.stop", _stop_bridge),
        ("ws_manager.close_all", _close_websockets),
        ("event_bus.disconnect", _disconnect_bus),
        ("valkey.aclose", _close_valkey),
        ("dispose_engine", _dispose_engine),
    ]

    for step_name, step_coro in cleanup_steps:
        try:
            await step_coro()
        except Exception:
            logger.exception("nexus.shutdown_step_failed", step=step_name)

    try:
        close_sentry()
    except Exception:
        logger.exception("nexus.shutdown_sentry_close_failed")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        SecurityHeadersMiddleware,
        config=SecurityHeadersConfig(),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    exempt_paths = tuple(
        p.strip() for p in settings.rate_limit_exempt_paths.split(",") if p.strip()
    )
    rate_limit_config = RateLimitConfig(
        default_per_minute=settings.rate_limit_per_minute,
        default_burst=settings.rate_limit_burst,
        exempt_paths=exempt_paths,
        role_tiers=settings.rate_limit_role_tiers_map,
        # Tight per-route cap on client-error reporting so a buggy
        # render loop in the frontend cannot accidentally DoS the
        # log pipeline. 30 req / minute / IP is well above any
        # legitimate ErrorBoundary trigger rate. ``trusted_proxy_depth``
        # stays at the safe default of 0; only raise it after a
        # trusted reverse proxy is verifiably the only path in.
        overrides={
            "/api/v1/client/errors": (30, 30),
        },
    )

    def _build_rate_limit_backend(app: FastAPI) -> Any:
        if not settings.rate_limit_valkey_enabled:
            return InMemoryBucketBackend()
        client = getattr(app.state, "valkey", None)
        if client is None:
            # Valkey not yet initialised (e.g. unit test building the
            # app without a lifespan). Fall back to in-memory so the
            # app stays usable; a warning is emitted so the operator
            # notices the misconfiguration in multi-pod deploys.
            logger.warning(
                "rate_limit.valkey_enabled_but_no_client",
                fallback="in_memory",
            )
            return InMemoryBucketBackend()
        return ValkeyBucketBackend(
            client=client,
            key_ttl_sec=settings.rate_limit_valkey_key_ttl_sec,
        )

    app.add_middleware(
        RateLimitMiddleware,
        config=rate_limit_config,
        backend=_build_rate_limit_backend(app),
    )
    # Hard cap on request body size — Starlette has no default. 1 MiB
    # is generous for every existing route and still well under the
    # log-bombing limits the per-route Pydantic models impose.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=1_048_576)
    app.add_middleware(CorrelationIdMiddleware)
    # Class-identity guard: the app factory MUST register the raw-ASGI
    # CorrelationIdMiddleware. The BaseHTTPMiddleware-based variant
    # (``BaseHTTPCorrelationIdMiddleware``) resets its structlog /
    # observability context bindings before streaming responses and
    # BackgroundTasks finish, which would leak and unbind ids, so it must
    # never be the default. Any future change that re-points the import at
    # a BaseHTTPMiddleware subclass trips this assertion immediately.
    from starlette.middleware.base import BaseHTTPMiddleware

    from engine.middleware.correlation import BaseHTTPCorrelationIdMiddleware

    assert CorrelationIdMiddleware is not BaseHTTPCorrelationIdMiddleware
    assert not issubclass(CorrelationIdMiddleware, BaseHTTPMiddleware), (
        "create_app() must register the raw-ASGI CorrelationIdMiddleware "
        "(engine.observability.middleware), not a BaseHTTPMiddleware variant"
    )
    # Stack order matters — HttpMetricsMiddleware is added last so it
    # wraps everything else and times the full request lifecycle. The
    # /metrics route itself is included so operators can monitor scrape
    # latency.
    app.add_middleware(HttpMetricsMiddleware)

    app.include_router(api_router)

    return app
