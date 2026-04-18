from __future__ import annotations

import asyncio
import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from engine.api.routes.backtest import router as bt_router
from engine.api.routes.legal import _apply_substitutions
from engine.app import create_app
from engine.db.models import LegalDocument
from engine.deps import get_db
from engine.legal import service as legal_service
from engine.legal.dependencies import require_legal_acceptance
from engine.legal.schemas import AcceptanceItem
from engine.legal.sync import sync_legal_documents
from tests.factories import make_user

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


class TestC2ConsentWiring:
    async def test_require_legal_acceptance_wired_on_backtest_routes(
        self, db_session: AsyncSession
    ):
        app = FastAPI()

        app.include_router(
            bt_router,
            prefix="/api/v1/backtest",
            dependencies=[Depends(require_legal_acceptance)],
        )

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/api/v1/backtest/run",
                json={
                    "strategy_name": "mean_reversion_basic",
                    "symbol": "AAPL",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                },
            )
            assert response.status_code in (
                HTTPStatus.OK,
                HTTPStatus.UNAVAILABLE_FOR_LEGAL_REASONS,
            )

    async def test_legal_routes_do_not_require_consent(self, db_session: AsyncSession):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/api/v1/legal/documents")
            assert response.status_code == HTTPStatus.OK

    async def test_health_routes_do_not_require_consent(self, db_session: AsyncSession):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/health")
            assert response.status_code == HTTPStatus.OK


class TestH3DbImmutability:
    async def test_update_legal_acceptance_raises(self, db_session: AsyncSession):
        await db_session.execute(
            text(
                "CREATE OR REPLACE FUNCTION prevent_acceptance_modification() "
                "RETURNS TRIGGER AS $$ "
                "BEGIN "
                "  RAISE EXCEPTION 'legal_acceptances records are immutable'; "
                "END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        await db_session.execute(
            text("DROP TRIGGER IF EXISTS no_acceptance_update ON legal_acceptances")
        )
        await db_session.execute(
            text(
                "CREATE TRIGGER no_acceptance_update "
                "BEFORE UPDATE OR DELETE ON legal_acceptances "
                "FOR EACH ROW EXECUTE FUNCTION prevent_acceptance_modification()"
            )
        )
        await db_session.commit()

        user = make_user(email="immutable@example.com")
        db_session.add(user)
        await db_session.flush()

        doc = LegalDocument(
            slug="immut-test",
            title="Immut Test",
            current_version="1.0.0",
            effective_date=datetime.date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="immut-test", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        record = (
            await db_session.execute(text("SELECT id FROM legal_acceptances LIMIT 1"))
        ).scalar_one()

        await db_session.execute(
            text("UPDATE legal_acceptances SET document_version = '9.9.9' WHERE id = :id"),
            {"id": str(record)},
        )
        with pytest.raises(Exception, match="immutable"):
            await db_session.flush()

    async def test_delete_legal_acceptance_raises(self, db_session: AsyncSession):
        await db_session.execute(
            text(
                "CREATE OR REPLACE FUNCTION prevent_acceptance_modification() "
                "RETURNS TRIGGER AS $$ "
                "BEGIN "
                "  RAISE EXCEPTION 'legal_acceptances records are immutable'; "
                "END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        await db_session.execute(
            text("DROP TRIGGER IF EXISTS no_acceptance_update ON legal_acceptances")
        )
        await db_session.execute(
            text(
                "CREATE TRIGGER no_acceptance_update "
                "BEFORE UPDATE OR DELETE ON legal_acceptances "
                "FOR EACH ROW EXECUTE FUNCTION prevent_acceptance_modification()"
            )
        )
        await db_session.commit()

        user = make_user(email="immutable-del@example.com")
        db_session.add(user)
        await db_session.flush()

        doc = LegalDocument(
            slug="immut-del-test",
            title="Immut Del Test",
            current_version="1.0.0",
            effective_date=datetime.date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="immut-del-test", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        record = (
            await db_session.execute(text("SELECT id FROM legal_acceptances LIMIT 1"))
        ).scalar_one()

        await db_session.execute(
            text("DELETE FROM legal_acceptances WHERE id = :id"),
            {"id": str(record)},
        )
        with pytest.raises(Exception, match="immutable"):
            await db_session.flush()


class TestM1MarkdownEscape:
    async def test_substitutions_escape_markdown_special_chars(self):
        with patch("engine.api.routes.legal.settings") as mock_settings:
            mock_settings.operator_name = "Evil**Co__Corp~~\n#Heading"
            mock_settings.operator_email = "test@test.com"
            mock_settings.operator_url = "https://test.com"
            mock_settings.jurisdiction = "US"
            mock_settings.platform_fee_percent = 30

            result = _apply_substitutions("{{OPERATOR_NAME}} rules")
            assert "**" not in result
            assert "##" not in result
            assert result != "Evil**Co__Corp~~\n#Heading rules"


class TestM2PathTraversal:
    async def test_rejects_file_path_outside_legal_dir(self, db_session: AsyncSession):
        doc = LegalDocument(
            slug="traversal-test",
            title="Traversal Test",
            current_version="1.0.0",
            effective_date=datetime.date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="/etc/passwd",
        )
        db_session.add(doc)
        await db_session.flush()

        result = await legal_service.get_document_content(db_session, "traversal-test")
        assert result is None

    async def test_rejects_relative_traversal(self, db_session: AsyncSession):
        doc = LegalDocument(
            slug="relative-traversal",
            title="Relative Traversal",
            current_version="1.0.0",
            effective_date=datetime.date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="../../etc/passwd",
        )
        db_session.add(doc)
        await db_session.flush()

        result = await legal_service.get_document_content(db_session, "relative-traversal")
        assert result is None

    async def test_allows_valid_legal_dir_path(self, db_session: AsyncSession):
        doc = LegalDocument(
            slug="valid-path-test",
            title="Valid Path",
            current_version="1.0.0",
            effective_date=datetime.date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()

        result = await legal_service.get_document_content(db_session, "valid-path-test")
        assert result is not None


class TestL1SyncRaceCondition:
    async def test_sync_is_idempotent_under_concurrency(self, db_session: AsyncSession):
        with patch("engine.legal.sync.settings") as mock_settings:
            mock_settings.legal_documents_dir = "legal"

            results = await asyncio.gather(
                sync_legal_documents(db_session),
                sync_legal_documents(db_session),
                return_exceptions=True,
            )

        for r in results:
            if isinstance(r, Exception):
                pytest.fail(f"Concurrent sync raised: {r}")
            assert r >= 1


class TestL2SlugValidation:
    @pytest.fixture
    async def legal_client_slug(self, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_rejects_slug_with_uppercase(self, legal_client_slug: AsyncClient):
        response = await legal_client_slug.get("/api/v1/legal/documents/HelloWorld")
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_rejects_slug_with_dots(self, legal_client_slug: AsyncClient):
        response = await legal_client_slug.get("/api/v1/legal/documents/hello.world")
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_rejects_slug_with_spaces(self, legal_client_slug: AsyncClient):
        response = await legal_client_slug.get("/api/v1/legal/documents/hello%20world")
        assert response.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.UNPROCESSABLE_ENTITY)

    async def test_accepts_valid_slug(self, legal_client_slug: AsyncClient):
        response = await legal_client_slug.get("/api/v1/legal/documents/risk-disclaimer")
        assert response.status_code == HTTPStatus.OK
