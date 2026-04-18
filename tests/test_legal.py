from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from engine.app import create_app
from engine.db.models import (
    DataProviderAttribution,
    LegalAcceptance,
    LegalDocument,
)
from engine.deps import get_db
from engine.legal import service as legal_service
from engine.legal.schemas import AcceptanceItem
from engine.legal.service import NUM_DOCS_ON_DISK
from engine.legal.sync import parse_front_matter, sync_legal_documents
from tests.factories import make_user

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


class TestFrontMatterParsing:
    def test_parse_valid_front_matter(self):
        content = '---\ntitle: "Risk Disclaimer"\nversion: "1.0.0"\n---\n\n# Content'
        meta = parse_front_matter(content)
        assert meta is not None
        assert meta["title"] == "Risk Disclaimer"
        assert meta["version"] == "1.0.0"

    def test_parse_missing_front_matter(self):
        content = "# Just a heading\n\nSome content"
        meta = parse_front_matter(content)
        assert meta is None

    def test_parse_empty_front_matter(self):
        content = "---\n\n---\n\n# Content"
        meta = parse_front_matter(content)
        assert meta == {}


class TestLegalDocumentSync:
    async def test_sync_registers_documents_from_legal_dir(self, db_session: AsyncSession):
        with patch("engine.legal.sync.settings") as mock_settings:
            mock_settings.legal_documents_dir = "legal"
            count = await sync_legal_documents(db_session)
            assert count >= NUM_DOCS_ON_DISK

        stmt = select(LegalDocument).where(LegalDocument.slug == "risk-disclaimer")
        result = await db_session.execute(stmt)
        doc = result.scalar_one_or_none()
        assert doc is not None
        assert doc.title == "Risk Disclaimer"
        assert doc.current_version == "1.0.0"
        assert doc.category == "trading"
        assert doc.requires_acceptance is True

    async def test_sync_updates_version(self, db_session: AsyncSession):
        doc = LegalDocument(
            slug="test-doc",
            title="Old Title",
            current_version="0.9.0",
            effective_date=__import__("datetime").date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        with patch("engine.legal.sync.settings") as mock_settings:
            mock_settings.legal_documents_dir = "legal"
            await sync_legal_documents(db_session)

        stmt = select(LegalDocument).where(LegalDocument.slug == "risk-disclaimer")
        result = await db_session.execute(stmt)
        updated = result.scalar_one()
        assert updated.current_version == "1.0.0"

    async def test_sync_handles_missing_directory(self, db_session: AsyncSession):
        with patch("engine.legal.sync.settings") as mock_settings:
            mock_settings.legal_documents_dir = "/nonexistent/path"
            count = await sync_legal_documents(db_session)
            assert count == 0


class TestLegalDocumentsAPI:
    @pytest.fixture
    async def legal_client(self, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_list_documents(self, legal_client: AsyncClient, db_session: AsyncSession):
        doc = LegalDocument(
            slug="test-doc",
            title="Test Document",
            current_version="1.0.0",
            effective_date=__import__("datetime").date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/documents")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert "documents" in data
        assert len(data["documents"]) >= 1
        slugs = [d["slug"] for d in data["documents"]]
        assert "test-doc" in slugs

    async def test_list_documents_filter_by_category(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        doc = LegalDocument(
            slug="cat-test",
            title="Cat Test",
            current_version="1.0.0",
            effective_date=__import__("datetime").date(2026, 4, 20),
            requires_acceptance=True,
            category="trading",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/documents?category=trading")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        for d in data["documents"]:
            assert d["category"] == "trading"

    async def test_get_document_content(self, legal_client: AsyncClient, db_session: AsyncSession):
        doc = LegalDocument(
            slug="content-test",
            title="Content Test",
            current_version="1.0.0",
            effective_date=__import__("datetime").date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/documents/content-test")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["slug"] == "content-test"
        assert data["title"] == "Content Test"
        assert "content_markdown" in data
        assert "{{OPERATOR_NAME}}" not in data["content_markdown"]

    async def test_get_document_not_found(self, legal_client: AsyncClient):
        response = await legal_client.get("/api/v1/legal/documents/nonexistent")
        assert response.status_code == HTTPStatus.NOT_FOUND

    async def test_accept_requires_auth(self, legal_client: AsyncClient):
        response = await legal_client.post(
            "/api/v1/legal/accept",
            json={
                "acceptances": [{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}]
            },
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    async def test_acceptances_me_requires_auth(self, legal_client: AsyncClient):
        response = await legal_client.get("/api/v1/legal/acceptances/me")
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    async def test_attributions_empty(self, legal_client: AsyncClient):
        response = await legal_client.get("/api/v1/legal/attributions")
        assert response.status_code == HTTPStatus.OK
        assert response.json()["attributions"] == []

    async def test_attributions_with_data(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        attr = DataProviderAttribution(
            provider_slug="test-provider",
            provider_name="Test Provider",
            attribution_text="Data by Test Provider",
            display_contexts=["data-feed", "chart"],
            is_active=True,
        )
        db_session.add(attr)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/attributions")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert len(data["attributions"]) == 1
        assert data["attributions"][0]["provider_slug"] == "test-provider"

    async def test_attributions_filter_by_context(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        attr = DataProviderAttribution(
            provider_slug="ctx-provider",
            provider_name="Ctx Provider",
            attribution_text="Data by Ctx",
            display_contexts=["data-feed"],
            is_active=True,
        )
        db_session.add(attr)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/attributions?context=chart")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert len(data["attributions"]) == 0

        response = await legal_client.get("/api/v1/legal/attributions?context=data-feed")
        data = response.json()
        assert len(data["attributions"]) == 1

    async def test_attributions_excludes_inactive(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        attr = DataProviderAttribution(
            provider_slug="inactive-provider",
            provider_name="Inactive",
            attribution_text="Inactive",
            display_contexts=[],
            is_active=False,
        )
        db_session.add(attr)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/attributions")
        data = response.json()
        slugs = [a["provider_slug"] for a in data["attributions"]]
        assert "inactive-provider" not in slugs


class TestLegalServiceAcceptance:
    async def _seed_doc(self, db_session: AsyncSession, slug: str) -> LegalDocument:
        doc = LegalDocument(
            slug=slug,
            title=slug.replace("-", " ").title(),
            current_version="1.0.0",
            effective_date=__import__("datetime").date(2026, 4, 20),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()
        return doc

    async def _seed_user(self, db_session: AsyncSession, email: str):
        user = make_user(email=email)
        db_session.add(user)
        await db_session.flush()
        return user

    async def test_record_acceptance(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "legal-test@example.com")
        await self._seed_doc(db_session, "svc-test-doc")

        accepted = await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="svc-test-doc", document_version="1.0.0")],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        assert len(accepted) == 1
        assert accepted[0].document_slug == "svc-test-doc"

    async def test_record_acceptance_idempotent(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "idempotent@example.com")
        await self._seed_doc(db_session, "idem-doc")

        items = [AcceptanceItem(document_slug="idem-doc", document_version="1.0.0")]
        await legal_service.record_acceptances(db_session, user.id, items, "127.0.0.1", "test")
        await legal_service.record_acceptances(db_session, user.id, items, "127.0.0.1", "test")

        count_stmt = (
            select(func.count())
            .select_from(LegalAcceptance)
            .where(
                LegalAcceptance.user_id == user.id,
                LegalAcceptance.document_slug == "idem-doc",
            )
        )
        result = await db_session.execute(count_stmt)
        assert result.scalar() == 1

    async def test_record_acceptance_invalid_document(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "invalid-doc@example.com")

        with pytest.raises(HTTPException) as exc_info:
            await legal_service.record_acceptances(
                db_session,
                user.id,
                [AcceptanceItem(document_slug="nonexistent", document_version="99.0.0")],
                "127.0.0.1",
                "test",
            )
        assert exc_info.value.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_get_pending_acceptances(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "pending@example.com")
        await self._seed_doc(db_session, "pending-doc")

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert "pending-doc" in slugs

    async def test_no_pending_after_acceptance(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "no-pending@example.com")
        await self._seed_doc(db_session, "accepted-doc")

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="accepted-doc", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert "accepted-doc" not in slugs

    async def test_pending_after_version_bump(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "version@example.com")
        doc = await self._seed_doc(db_session, "version-doc")

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="version-doc", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        doc.current_version = "2.0.0"
        await db_session.flush()

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert "version-doc" in slugs

    async def test_list_user_acceptances(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "history@example.com")
        await self._seed_doc(db_session, "history-doc")

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="history-doc", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        records = await legal_service.list_user_acceptances(db_session, user.id)
        assert len(records) >= 1
        assert any(r.document_slug == "history-doc" for r in records)

    async def test_context_onboarding_for_first_acceptance(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "onboard-ctx@example.com")
        await self._seed_doc(db_session, "ctx-doc")

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="ctx-doc", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        records = await legal_service.list_user_acceptances(db_session, user.id, "ctx-doc")
        assert records[0].context == "onboarding"

    async def test_context_re_acceptance_for_version_change(self, db_session: AsyncSession):
        user = await self._seed_user(db_session, "re-ctx@example.com")
        doc = await self._seed_doc(db_session, "re-ctx-doc")

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="re-ctx-doc", document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        doc.current_version = "2.0.0"
        await db_session.flush()

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug="re-ctx-doc", document_version="2.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        records = await legal_service.list_user_acceptances(db_session, user.id, "re-ctx-doc")
        assert len(records) == 2  # noqa: PLR2004
        v2_record = next(r for r in records if r.document_version == "2.0.0")
        assert v2_record.context == "re-acceptance"
