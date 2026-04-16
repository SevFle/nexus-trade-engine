"""
Nexus Trade Engine — Main application entry point.
"""

from contextlib import asynccontextmanager

import structlog
from api.routes import backtest, marketplace, portfolio, strategies
from config import get_settings
from db.session import close_db, init_db
from events.bus import EventBus
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from plugins.registry import PluginRegistry

logger = structlog.get_logger()
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("nexus.startup", environment=settings.environment)

    # Initialize database connection pool
    await init_db()

    # Initialize the event bus
    app.state.event_bus = EventBus(redis_url=settings.redis_url)
    await app.state.event_bus.connect()

    # Load installed strategy plugins
    app.state.plugin_registry = PluginRegistry(plugin_dir=settings.plugin_dir)
    loaded = await app.state.plugin_registry.discover_and_load()
    logger.info("nexus.plugins_loaded", count=loaded)

    yield

    # Shutdown
    logger.info("nexus.shutdown")
    await app.state.event_bus.disconnect()
    await close_db()


app = FastAPI(
    title=settings.app_name,
    description="AI-native plugin trading framework with full cost modeling.",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ──
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
        "environment": settings.environment,
        "execution_mode": settings.default_execution_mode,
    }
