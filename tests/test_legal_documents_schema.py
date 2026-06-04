"""Schema & infrastructure regression tests for the ``legal_documents`` table.

These tests exist to break the "no such table: legal_documents" fix-loop that
has historically produced a string of conftest.py patches and dependency
mocks across multiple commits.

The loop happened because nothing was pinning the *real* contract:

1. ``LegalDocument`` / ``LegalAcceptance`` must be registered against
   ``Base.metadata`` (i.e. imported before any ``create_all`` runs).
2. ``Base.metadata.create_all`` on the SQLite test engine must actually
   create the ``legal_documents`` table.
3. ``requires_acceptance`` is a Boolean column — its ``is_(True)`` query
   must work on SQLite (which stores booleans as 0/1) and on Postgres.
4. ``require_legal_acceptance`` must function end-to-end against a real
   session without any dependency override.

If any of these break, the conftest-bypass pattern from the loop becomes
tempting again — but these tests will fail first, forcing the *root cause*
to be fixed rather than papered over.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from engine.db.models import Base, LegalAcceptance, LegalDocument
from engine.legal.dependencies import require_legal_acceptance
from engine.legal.service import get_pending_acceptances

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# SQLite has no JSONB; mirror the conftest override so create_all can run.
compiles(JSONB, "sqlite")(lambda type_, compiler, **kw: "TEXT")


def _sqlite_engine():
    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# ---------------------------------------------------------------------------
# 1. Model registration
# ---------------------------------------------------------------------------
class TestLegalModelRegistration:
    """Pin ``LegalDocument`` / ``LegalAcceptance`` to ``Base.metadata``.

    If either model is removed from ``engine.db.models`` (or accidentally
    imported under a fresh ``Base``), the test DB schema will silently lose
    the tables and every downstream test will 500 — exactly the failure
    mode that triggered the loop.
    """

    def test_legal_documents_table_registered_in_metadata(self) -> None:
        assert "legal_documents" in Base.metadata.tables, (
            "LegalDocument must be registered on Base.metadata — "
            "engine/db/models.py must export it and import-time must run "
            "before any test calls Base.metadata.create_all()."
        )

    def test_legal_acceptances_table_registered_in_metadata(self) -> None:
        assert "legal_acceptances" in Base.metadata.tables, (
            "LegalAcceptance must be registered on Base.metadata — same "
            "import path as LegalDocument."
        )

    def test_legal_documents_table_uses_expected_columns(self) -> None:
        """Lock the column shape — additions are fine, removals are not."""
        table = Base.metadata.tables["legal_documents"]
        expected = {
            "id",
            "slug",
            "title",
            "current_version",
            "effective_date",
            "requires_acceptance",
            "category",
            "display_order",
            "file_path",
            "created_at",
            "updated_at",
        }
        missing = expected - set(table.columns.keys())
        assert not missing, f"legal_documents is missing columns: {missing}"

    def test_legal_acceptances_table_uses_expected_columns(self) -> None:
        table = Base.metadata.tables["legal_acceptances"]
        expected = {
            "id",
            "user_id",
            "document_slug",
            "document_version",
            "accepted_at",
            "ip_address",
            "user_agent",
            "context",
            "revoked_at",
        }
        missing = expected - set(table.columns.keys())
        assert not missing, f"legal_acceptances is missing columns: {missing}"

    def test_legal_documents_slug_is_unique(self) -> None:
        table = Base.metadata.tables["legal_documents"]
        slug_col = table.columns["slug"]
        assert slug_col.unique is True, "slug must remain unique"

    def test_requires_acceptance_is_boolean(self) -> None:
        """SQLite stores booleans as integers but the SQLAlchemy column type
        must remain ``Boolean`` so the dialect translator emits the right
        predicate (``IS 1`` on SQLite, ``IS true`` on Postgres). Using a
        bare ``Integer`` here would silently break ``.is_(True)``."""
        from sqlalchemy import Boolean

        table = Base.metadata.tables["legal_documents"]
        col_type = table.columns["requires_acceptance"].type
        assert isinstance(col_type, Boolean), (
            f"requires_acceptance must be Boolean, got {type(col_type).__name__} — "
            "SQLite boolean abstraction relies on this annotation."
        )


# ---------------------------------------------------------------------------
# 2. create_all actually creates the table on SQLite
# ---------------------------------------------------------------------------
class TestTableCreation:
    """``Base.metadata.create_all`` must materialise the legal tables.

    This is what the conftest ``test_engine`` fixture relies on — if the
    model is unregistered, ``create_all`` silently skips it and downstream
    queries blow up with ``no such table: legal_documents``.
    """

    async def test_create_all_materializes_legal_documents_on_sqlite(self) -> None:
        engine = _sqlite_engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            def _table_names(sync_conn):
                return inspect(sync_conn).get_table_names()

            async with engine.connect() as conn:
                names = await conn.run_sync(_table_names)

            assert "legal_documents" in names
            assert "legal_acceptances" in names
        finally:
            await engine.dispose()

    async def test_create_all_idempotent(self) -> None:
        """Running create_all twice must not raise — many tests call this
        in module-scoped fixtures and we don't want flaky behaviour."""
        engine = _sqlite_engine()
        try:
            for _ in range(2):
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                names = {row[0] for row in result}
            assert {"legal_documents", "legal_acceptances"} <= names
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Boolean column behaviour — the SQLite-specific IS true / IS 1 fixpoint
# ---------------------------------------------------------------------------
class TestRequiresAcceptanceBoolean:
    """Exercise the ``requires_acceptance`` column on SQLite.

    The ``get_pending_acceptances`` query is
    ``LegalDocument.requires_acceptance.is_(True)``. On SQLite this must
    compile to ``legal_documents.requires_acceptance IS 1`` and on Postgres
    to ``IS true``. SQLAlchemy handles this automatically *if* the column
    type is ``Boolean``; the previous test class guards the type, this
    class guards the runtime behaviour.
    """

    @pytest.fixture
    async def sqlite_session(self) -> AsyncIterator[AsyncSession]:
        engine = _sqlite_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            yield session
        await engine.dispose()

    async def _seed(
        self,
        session: AsyncSession,
        slug: str,
        requires: bool,
        version: str = "1.0.0",
    ) -> LegalDocument:
        doc = LegalDocument(
            slug=slug,
            title=slug,
            current_version=version,
            effective_date=datetime.date(2026, 1, 1),
            requires_acceptance=requires,
            category="general",
            display_order=0,
            file_path="legal/risk-disclaimer.md",
        )
        session.add(doc)
        await session.flush()
        return doc

    async def test_is_true_filter_returns_only_required(
        self, sqlite_session: AsyncSession
    ) -> None:
        await self._seed(sqlite_session, "required-doc", requires=True)
        await self._seed(sqlite_session, "optional-doc", requires=False)

        stmt = select(LegalDocument).where(LegalDocument.requires_acceptance.is_(True))
        result = await sqlite_session.execute(stmt)
        slugs = {d.slug for d in result.scalars().all()}
        assert slugs == {"required-doc"}, (
            "is_(True) must filter to requires_acceptance=True rows on SQLite — "
            "this is the exact predicate get_pending_acceptances relies on."
        )

    async def test_is_false_filter_returns_only_optional(
        self, sqlite_session: AsyncSession
    ) -> None:
        await self._seed(sqlite_session, "req-t", requires=True)
        await self._seed(sqlite_session, "req-f", requires=False)

        stmt = select(LegalDocument).where(LegalDocument.requires_acceptance.is_(False))
        result = await sqlite_session.execute(stmt)
        slugs = {d.slug for d in result.scalars().all()}
        assert slugs == {"req-f"}

    async def test_get_pending_acceptances_skips_optional_docs(
        self, sqlite_session: AsyncSession
    ) -> None:
        """End-to-end check on the service function that powers the
        dependency — verifies the boolean filter survives the SQLAlchemy
        SQLite compilation path."""
        await self._seed(sqlite_session, "must-accept", requires=True)
        await self._seed(sqlite_session, "info-only", requires=False)

        user_id = uuid.uuid4()
        pending = await get_pending_acceptances(sqlite_session, user_id)
        slugs = {p.slug for p in pending}
        assert "must-accept" in slugs
        assert "info-only" not in slugs

    async def test_default_requires_acceptance_is_true(
        self, sqlite_session: AsyncSession
    ) -> None:
        """Mapped column declares ``default=True`` — verify the default
        fires on INSERT so callers that forget to set it don't silently
        disable consent enforcement."""
        doc = LegalDocument(
            slug="default-test",
            title="Default Test",
            current_version="1.0.0",
            effective_date=datetime.date(2026, 1, 1),
            # requires_acceptance intentionally omitted
            category="general",
            display_order=0,
            file_path="legal/risk-disclaimer.md",
        )
        sqlite_session.add(doc)
        await sqlite_session.flush()
        await sqlite_session.refresh(doc)
        assert doc.requires_acceptance is True


# ---------------------------------------------------------------------------
# 4. Dependency without override — the loop's regression target
# ---------------------------------------------------------------------------
class TestRequireLegalAcceptanceWithoutOverride:
    """``require_legal_acceptance`` must work against a real session.

    The historical loop added conftest dependency overrides precisely to
    avoid exercising this path. These tests pin the opposite contract: the
    dependency must run cleanly when the table exists, must return ``None``
    when no user is configured, and must raise 451 when there's a pending
    re-acceptance.
    """

    @pytest.fixture
    async def session(self) -> AsyncIterator[AsyncSession]:
        engine = _sqlite_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            yield s
        await engine.dispose()

    async def test_dependency_noop_when_placeholder_unset(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """The current contract: when no user is wired up, the dependency
        returns immediately without touching the DB. This is what allows
        the public read-only routes to work pre-auth."""
        from engine.legal import dependencies

        monkeypatch.setattr(dependencies, "_placeholder_user_id", None)
        # Must not raise — and must not require legal_documents to be populated.
        result = await require_legal_acceptance(db=session)  # type: ignore[call-arg]
        assert result is None

    async def test_dependency_raises_451_when_pending(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """End-to-end: with a placeholder user and an unaccepted required
        document, the dependency raises HTTP 451."""
        from engine.legal import dependencies

        user_id = uuid.uuid4()
        monkeypatch.setattr(dependencies, "_placeholder_user_id", user_id)

        session.add(
            LegalDocument(
                slug="must-accept-dep",
                title="Must Accept",
                current_version="1.0.0",
                effective_date=datetime.date(2026, 1, 1),
                requires_acceptance=True,
                category="general",
                display_order=0,
                file_path="legal/risk-disclaimer.md",
            )
        )
        await session.flush()

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await require_legal_acceptance(db=session)  # type: ignore[call-arg]
        assert exc.value.status_code == 451
        assert exc.value.detail["code"] == "legal_re_acceptance_required"
        assert "must-accept-dep" in exc.value.detail["documents"]

    async def test_dependency_passes_when_all_accepted(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Once a user accepts every required doc at the current version,
        the dependency must return ``None`` — proving the table round-trip
        (insert acceptance + query pending) works on SQLite."""
        from engine.legal import dependencies

        user_id = uuid.uuid4()
        monkeypatch.setattr(dependencies, "_placeholder_user_id", user_id)

        from engine.db.models import User

        session.add(
            User(
                id=user_id,
                email="dep-test@example.com",
                display_name="Dep Test",
                is_active=True,
            )
        )
        session.add(
            LegalDocument(
                slug="accepted-dep",
                title="Accepted",
                current_version="1.0.0",
                effective_date=datetime.date(2026, 1, 1),
                requires_acceptance=True,
                category="general",
                display_order=0,
                file_path="legal/risk-disclaimer.md",
            )
        )
        await session.flush()

        session.add(
            LegalAcceptance(
                user_id=user_id,
                document_slug="accepted-dep",
                document_version="1.0.0",
                accepted_at=datetime.datetime.now(datetime.UTC),
                ip_address="127.0.0.1",
                user_agent="test-agent",
                context="onboarding",
            )
        )
        await session.flush()

        result = await require_legal_acceptance(db=session)  # type: ignore[call-arg]
        assert result is None


# ---------------------------------------------------------------------------
# 5. Migration ↔ model parity — guard against future schema drift
# ---------------------------------------------------------------------------
class TestMigrationModelParity:
    """The Alembic migration and the ORM model must agree on column shape.

    If a future migration adds a column that the model forgets (or vice
    versa) the test DB (created via ``create_all``) and the production DB
    (managed by Alembic) will silently drift, causing exactly the kind of
    "works on dev, blows up in CI" loop we're trying to prevent.
    """

    def test_alembic_legal_documents_revision_exists(self) -> None:
        """The migration that creates ``legal_documents`` must exist and be
        reachable from the alembic version tree."""
        import importlib

        module = importlib.import_module(
            "engine.db.migrations.versions.004_legal_documents"
        )
        assert module.revision == "004_legal_documents"
        assert module.down_revision is not None, (
            "004_legal_documents must chain off a prior revision so it runs "
            "as part of `alembic upgrade head`."
        )

    def test_migration_and_model_columns_agree(self) -> None:
        """Cross-check the columns the migration creates vs the columns the
        ORM model declares. Allows additions in either direction but fails
        on removals."""
        import importlib

        migration = importlib.import_module(
            "engine.db.migrations.versions.004_legal_documents"
        )

        # Introspect what create_table would build by capturing op commands
        # is heavy — instead we just import the model table and assert that
        # the migration module defines an `upgrade` that mentions every
        # column the model has. This catches the most common drift case:
        # someone adds a column to the model but forgets the migration.
        import inspect

        source = inspect.getsource(migration)
        model_cols = set(Base.metadata.tables["legal_documents"].columns.keys())
        missing_from_migration = {
            c for c in model_cols if f'"{c}"' not in source and f"'{c}'" not in source
        }
        assert not missing_from_migration, (
            f"Columns present on LegalDocument model but not in the 004 migration: "
            f"{missing_from_migration}. Add them or write a new migration."
        )
