"""Shared test fixtures for the Nexus Trade Engine test suite.

This conftest provides:
- ``client``: an httpx AsyncClient wired to a real FastAPI app with auth bypassed.
- ``test_engine``: a session-scoped SQLAlchemy async engine (SQLite in-memory by
  default; switches to the configured ``database_url`` when it contains ``test``).
- ``db_session``: a per-test database session backed by a nested transaction that
  rolls back after each test, keeping the schema across tests without leaking data.
- ``db_client``: an httpx client with the DB session injected, for integration tests
  that need real DB rows visible to the app.
 - ``_bypass_auth``: an autouse fixture that patches ``FastAPI.__init__`` so every
  new app instance gets a dependency override for ``get_current_user``.  Files
  matching ``test_auth*`` or ``*_requires_auth`` opt out to exercise real auth.

Intentionally empty — session-scoped asyncio loop is configured in pyproject.toml.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from engine.api.auth.dependency import get_current_user
from engine.app import create_app
from engine.config import settings
from engine.db.models import Base, User
from engine.deps import get_db
from engine.legal.dependencies import require_legal_acceptance


async def _noop_legal_acceptance() -> None:
    """Test-only override that skips legal acceptance enforcement.

    The real dependency queries the ``legal_documents`` table which most
    test databases do not provision.  Tests that exercise legal acceptance
    do so by calling :func:`require_legal_acceptance` directly rather than
    through the FastAPI dependency graph, so overriding the wired dependency
    is safe.
    """
    return

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


FAKE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _fake_authenticated_user(role: str = "admin") -> User:
    return User(
        id=FAKE_USER_ID,
        email="test@example.com",
        display_name="Test User",
        is_active=True,
        role=role,
        auth_provider="local",
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
    bypass_user = "test_auth" not in nodeid and "_requires_auth" not in nodeid

    from fastapi import FastAPI

    fake = _fake_authenticated_user()
    original_init = FastAPI.__init__

    if bypass_user:
        # Bypass both auth and legal acceptance for the majority of tests.
        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.dependency_overrides[get_current_user] = lambda: fake
            self.dependency_overrides[require_legal_acceptance] = _noop_legal_acceptance

        monkeypatch.setattr(FastAPI, "__init__", patched_init)
    else:
        # Auth tests exercise real auth but still must not hit the
        # ``legal_documents`` table — legal acceptance is out of scope here.
        def patched_init_legal_only(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.dependency_overrides[require_legal_acceptance] = _noop_legal_acceptance

        monkeypatch.setattr(FastAPI, "__init__", patched_init_legal_only)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_current_user] = _fake_authenticated_user
    app.dependency_overrides[require_legal_acceptance] = _noop_legal_acceptance
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _build_test_engine() -> tuple[AsyncEngine, bool]:
    db_url = settings.database_url
    if db_url and "test" in db_url.lower():
        return create_async_engine(db_url, echo=False), False

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    from sqlalchemy.ext.compiler import compiles

    compiles(JSONB, "sqlite")(lambda type_, compiler, **kw: "TEXT")

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine, True


@pytest.fixture(scope="session")
async def test_engine():
    engine, is_sqlite = _build_test_engine()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        if is_sqlite:
            await conn.run_sync(Base.metadata.drop_all)
        else:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncIterator[AsyncSession]:
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        await connection.begin_nested()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
        )
        yield session
        await session.close()
        if transaction.is_active:
            await transaction.rollback()


@pytest.fixture
async def db_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = _fake_authenticated_user
    app.dependency_overrides[require_legal_acceptance] = _noop_legal_acceptance
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
