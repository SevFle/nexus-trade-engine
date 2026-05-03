from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engine.api.auth.dependency import get_current_user
from engine.app import create_app
from engine.config import settings
from engine.db.models import Base, User
from engine.deps import get_db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


FAKE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _fake_authenticated_user(role: str = "admin") -> User:
    return User(
        id=FAKE_USER_ID,
        email="test@example.com",
        display_name="Test User",
        is_active=True,
        role=role,
    )


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _bypass_auth(request, monkeypatch):
    """Globally bypass Bearer-token auth in tests. Patches FastAPI so every
    new app gets a dependency_override for get_current_user, covering tests
    that build their own isolated FastAPI instances. Tests in test_auth*
    opt out so they exercise real auth behavior."""
    nodeid = request.node.nodeid
    if "test_auth" in nodeid or "_requires_auth" in nodeid:
        return

    from fastapi import FastAPI

    fake = _fake_authenticated_user()
    original_init = FastAPI.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.dependency_overrides[get_current_user] = lambda: fake

    monkeypatch.setattr(FastAPI, "__init__", patched_init)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_current_user] = _fake_authenticated_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
async def test_engine():
    if settings.database_url and "test" in settings.database_url.lower():
        db_url = settings.database_url
        _is_sqlite = False
    else:
        db_url = "sqlite+aiosqlite://"
        _is_sqlite = True

    if _is_sqlite:
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = JSON()

    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        if _is_sqlite:
            await conn.run_sync(Base.metadata.drop_all)
        else:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def db_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = _fake_authenticated_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
