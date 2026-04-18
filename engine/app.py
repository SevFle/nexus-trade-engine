from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from valkey.asyncio import Valkey

from engine.api.router import api_router
from engine.config import settings
from engine.db.session import dispose_engine, get_session_factory
from engine.observability.logging import setup_logging
from engine.observability.tracing import setup_tracing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    setup_tracing()
    app.state.valkey = Valkey.from_url(settings.valkey_url)

    if not settings.is_test:
        try:
            from engine.legal.sync import sync_legal_documents  # noqa: PLC0415

            session_factory = get_session_factory()
            async with session_factory() as session:
                await sync_legal_documents(session)
                await session.commit()
        except Exception:
            import structlog  # noqa: PLC0415

            structlog.get_logger().exception("legal.sync_failed_on_startup")

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
