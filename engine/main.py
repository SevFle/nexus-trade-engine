"""
Nexus Trade Engine — Main application entry point.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from engine.api.routes import backtest, marketplace, portfolio, strategies
from engine.config import settings
from engine.db.session import dispose_engine, init_db
from engine.events.bus import EventBus
from engine.plugins.registry import PluginRegistry
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle."""
    logger.info("nexus.startup", environment=settings.app_env)

    await init_db()

    app.state.event_bus = EventBus(redis_url=settings.valkey_url)
    await app.state.event_bus.connect()

    app.state.plugin_registry = PluginRegistry()
    loaded = await app.state.plugin_registry.discover_and_load()
    logger.info("nexus.plugins_loaded", count=loaded)

    yield

    logger.info("nexus.shutdown")
    await app.state.event_bus.disconnect()
    await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    description="AI-native plugin trading framework with full cost modeling.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(portfolio.router, prefix="/api/v1/portfolio", tags=["Portfolio"])
app.include_router(strategies.router, prefix="/api/v1/strategies", tags=["Strategies"])
app.include_router(backtest.router, prefix="/api/v1/backtest", tags=["Backtest"])
app.include_router(marketplace.router, prefix="/api/v1/marketplace", tags=["Marketplace"])


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "engine": settings.app_name,
        "version": "0.1.0",
        "environment": settings.app_env,
    }
