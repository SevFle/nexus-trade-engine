from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from valkey.asyncio import Valkey

from engine.api.auth.local import LocalAuthProvider
from engine.api.auth.registry import AuthProviderRegistry
from engine.api.router import api_router
from engine.config import settings
from engine.data.providers import (
    AssetClass,
    ProviderRegistration,
    YahooDataProvider,
    configure_from_file,
    get_registry,
)
from engine.db.session import dispose_engine, get_session_factory
from engine.legal.sync import sync_legal_documents
from engine.observability.logging import setup_logging
from engine.observability.middleware import CorrelationIdMiddleware
from engine.observability.tracing import setup_tracing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
            logger.exception(
                "data_provider.bootstrap.failed", path=settings.data_providers_config
            )
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    setup_tracing()

    if not settings.is_test and not settings.secret_key:
        msg = "NEXUS_SECRET_KEY must be set outside the test environment"
        raise ValueError(msg)

    app.state.valkey = Valkey.from_url(settings.valkey_url)
    app.state.auth_registry = _build_auth_registry()
    logger.info("auth.providers_loaded", providers=list(app.state.auth_registry.providers.keys()))
    _configure_data_providers()
    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            count = await sync_legal_documents(db)
            await db.commit()
        if count > 0:
            logger.info("legal.sync_complete", documents_synced=count)
    except Exception:
        logger.exception("legal.sync_failed")
    yield
    await app.state.valkey.aclose()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(api_router)

    return app
