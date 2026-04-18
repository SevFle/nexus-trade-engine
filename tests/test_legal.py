from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from engine.app import create_app
from engine.db.models import (
    DataProviderAttribution,
    LegalAcceptance,
    LegalDocument,
)
from engine.deps import get_db
from engine.legal.service import (
    get_pending_acceptances,
    get_user_acceptances,
    list_documents,
    record_acceptances,
)
from engine.legal.sync import (
    apply_substitutions,
    parse_front_matter,
    slug_from_filename,
    sync_legal_documents,
)
from tests.factories import make_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNAUTHORIZED = 401
NUM_LEGAL_DOCS = 6


@pytest.fixture
async def legal_client(db_session: AsyncSession) -> AsyncClient:
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestLegalDocumentSync:
    async def test_parse_front_matter(self):
        content = '---\ntitle: "Test Doc"\nversion: "1.0.0"\n---\n\nBody text'
        meta, body = parse_front_matter(content)
        assert meta["title"] == "Test Doc"
        assert meta["version"] == "1.0.0"
        assert "Body text" in body

    async def test_parse_front_matter_invalid(self):
        with pytest.raises(ValueError, match="front-matter"):
            parse_front_matter("no front matter here")

    async def test_slug_from_filename(self):
        assert slug_from_filename("risk-disclaimer.md") == "risk-disclaimer"
        assert slug_from_filename("terms-of-service.md") == "terms-of-service"

    async def test_apply_substitutions(self):
        text = "{{OPERATOR_NAME}} provides this platform."
        result = apply_substitutions(text)
        assert "Nexus Trade Engine" in result
        assert "{{OPERATOR_NAME}}" not in result

    async def test_sync_creates_documents(self, db_session: AsyncSession):
        docs = await sync_legal_documents(db_session)
        await db_session.flush()

        slugs = {d.slug for d in docs}
        assert "risk-disclaimer" in slugs
        assert "terms-of-service" in slugs
        assert "privacy-policy" in slugs
        assert "eula" in slugs
        assert "marketplace-eula" in slugs
        assert "data-provider-attributions" in slugs

    async def test_sync_upsert_no_duplicate(self, db_session: AsyncSession):
        docs1 = await sync_legal_documents(db_session)
        await db_session.flush()
        count_after_first = len(docs1)

        docs2 = await sync_legal_documents(db_session)
        await db_session.flush()
        assert len(docs2) == count_after_first

    async def test_sync_detects_version_change(self, db_session: AsyncSession):
        await sync_legal_documents(db_session)
        await db_session.flush()

        doc = await db_session.execute(
            LegalDocument.__table__.select().where(LegalDocument.slug == "risk-disclaimer")
        )
        row = doc.first()
        assert row is not None

        await db_session.execute(
            LegalDocument.__table__.update()
            .where(LegalDocument.slug == "risk-disclaimer")
            .values(current_version="0.9.0")
        )
        await db_session.flush()

        docs = await sync_legal_documents(db_session)
        await db_session.flush()

        risk_doc = next(d for d in docs if d.slug == "risk-disclaimer")
        assert risk_doc.current_version == "1.0.0"


class TestLegalDocumentAPI:
    async def test_list_documents(self, legal_client: AsyncClient, db_session: AsyncSession):
        await sync_legal_documents(db_session)
        await db_session.commit()

        resp = await legal_client.get("/api/v1/legal/documents")
        assert resp.status_code == HTTP_OK
        data = resp.json()
        assert "documents" in data
        assert len(data["documents"]) >= NUM_LEGAL_DOCS

    async def test_list_documents_filter_category(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        await sync_legal_documents(db_session)
        await db_session.commit()

        resp = await legal_client.get("/api/v1/legal/documents?category=trading")
        assert resp.status_code == HTTP_OK
        data = resp.json()
        assert all(d["category"] == "trading" for d in data["documents"])

    async def test_get_document_content(self, legal_client: AsyncClient, db_session: AsyncSession):
        await sync_legal_documents(db_session)
        await db_session.commit()

        resp = await legal_client.get("/api/v1/legal/documents/risk-disclaimer")
        assert resp.status_code == HTTP_OK
        data = resp.json()
        assert data["slug"] == "risk-disclaimer"
        assert data["title"] == "Risk Disclaimer"
        assert "content_markdown" in data
        assert "Nexus Trade Engine" in data["content_markdown"]
        assert data["requires_acceptance"] is True

    async def test_get_document_not_found(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        await sync_legal_documents(db_session)
        await db_session.commit()

        resp = await legal_client.get("/api/v1/legal/documents/nonexistent")
        assert resp.status_code == HTTP_NOT_FOUND

    async def test_attributions_empty(self, legal_client: AsyncClient):
        resp = await legal_client.get("/api/v1/legal/attributions")
        assert resp.status_code == HTTP_OK
        data = resp.json()
        assert "attributions" in data
        assert len(data["attributions"]) == 0

    async def test_attributions_with_data(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        attr = DataProviderAttribution(
            provider_slug="polygon-io",
            provider_name="Polygon.io",
            attribution_text="Market data provided by Polygon.io",
            attribution_url="https://polygon.io",
            display_contexts=["data-feed", "backtest-result"],
        )
        db_session.add(attr)
        await db_session.commit()

        resp = await legal_client.get("/api/v1/legal/attributions")
        assert resp.status_code == HTTP_OK
        data = resp.json()
        assert len(data["attributions"]) == 1
        assert data["attributions"][0]["provider_slug"] == "polygon-io"

    async def test_attributions_filter_context(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        attr = DataProviderAttribution(
            provider_slug="polygon-io",
            provider_name="Polygon.io",
            attribution_text="Market data provided by Polygon.io",
            display_contexts=["data-feed", "backtest-result"],
        )
        db_session.add(attr)
        await db_session.commit()

        resp = await legal_client.get("/api/v1/legal/attributions?context=data-feed")
        assert resp.status_code == HTTP_OK
        assert len(resp.json()["attributions"]) == 1

        resp2 = await legal_client.get("/api/v1/legal/attributions?context=nonexistent")
        assert resp2.status_code == HTTP_OK
        assert len(resp2.json()["attributions"]) == 0

    async def test_accept_requires_auth(self, legal_client: AsyncClient):
        resp = await legal_client.post(
            "/api/v1/legal/accept",
            json={
                "acceptances": [{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}]
            },
        )
        assert resp.status_code == HTTP_UNAUTHORIZED

    async def test_acceptances_me_requires_auth(self, legal_client: AsyncClient):
        resp = await legal_client.get("/api/v1/legal/acceptances/me")
        assert resp.status_code == HTTP_UNAUTHORIZED


class TestLegalAcceptanceService:
    async def test_record_acceptance(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        accepted = await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        assert len(accepted) == 1
        assert accepted[0]["document_slug"] == "risk-disclaimer"
        assert accepted[0]["document_version"] == "1.0.0"
        assert accepted[0]["accepted_at"] is not None

    async def test_record_acceptance_idempotent(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        accepted2 = await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        assert len(accepted2) == 1

        count_result = await db_session.execute(
            LegalAcceptance.__table__.select().where(LegalAcceptance.user_id == user.id)
        )
        rows = count_result.fetchall()
        assert len(rows) == 1

    async def test_acceptance_context_detection(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        records = await get_user_acceptances(db_session, user.id)
        assert len(records) == 1
        assert records[0].context == "onboarding"

    async def test_get_pending_acceptances(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        pending = await get_pending_acceptances(db_session, user.id)
        pending_slugs = {p.slug for p in pending}
        assert "risk-disclaimer" in pending_slugs
        assert "terms-of-service" in pending_slugs

    async def test_get_pending_acceptances_after_accept(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        pending = await get_pending_acceptances(db_session, user.id)
        pending_slugs = {p.slug for p in pending}
        assert "risk-disclaimer" not in pending_slugs

    async def test_list_documents_with_user_acceptance_status(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        docs = await list_documents(db_session, user_id=user.id)
        risk_doc = next(d for d in docs if d["slug"] == "risk-disclaimer")
        assert risk_doc["accepted"] is True
        assert risk_doc["accepted_version"] == "1.0.0"
        assert risk_doc["needs_re_acceptance"] is False

    async def test_needs_re_acceptance_after_version_bump(self, db_session: AsyncSession):
        user = make_user()
        db_session.add(user)
        await sync_legal_documents(db_session)
        await db_session.flush()

        await record_acceptances(
            db_session,
            user_id=user.id,
            acceptances=[{"document_slug": "risk-disclaimer", "document_version": "1.0.0"}],
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        await db_session.flush()

        await db_session.execute(
            LegalDocument.__table__.update()
            .where(LegalDocument.slug == "risk-disclaimer")
            .values(current_version="2.0.0")
        )
        await db_session.flush()

        docs = await list_documents(db_session, user_id=user.id)
        risk_doc = next(d for d in docs if d["slug"] == "risk-disclaimer")
        assert risk_doc["accepted"] is True
        assert risk_doc["accepted_version"] == "1.0.0"
        assert risk_doc["needs_re_acceptance"] is True
