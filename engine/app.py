from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from valkey.asyncio import Valkey

from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.api.auth.google import GoogleAuthProvider
from engine.api.auth.ldap import LDAPAuthProvider
from engine.api.auth.local import LocalAuthProvider
from engine.api.auth.oidc import OIDCAuthProvider
from engine.api.auth.registry import AuthProviderRegistry
from engine.api.router import api_router
from engine.config import settings
from engine.db.session import dispose_engine
from engine.observability.logging import setup_logging
from engine.observability.tracing import setup_tracing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger()


def _build_auth_registry() -> AuthProviderRegistry:
    registry = AuthProviderRegistry()
    for name in settings.enabled_providers:
        if name == "local":
            registry.register(LocalAuthProvider())
        elif name == "google":
            registry.register(GoogleAuthProvider())
        elif name == "github":
            registry.register(GitHubAuthProvider())
        elif name == "oidc":
            registry.register(OIDCAuthProvider())
        elif name == "ldap":
            registry.register(LDAPAuthProvider())
        else:
            logger.warning("auth.unknown_provider", provider=name)
    logger.info("auth.providers_loaded", providers=[p.name for p in registry.providers])
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    setup_tracing()
    app.state.valkey = Valkey.from_url(settings.valkey_url)
    app.state.auth_registry = _build_auth_registry()
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

    app.include_router(api_router)

    return app
