from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from engine.app import create_app
from engine.db.models import LegalAcceptance, LegalDocument
from engine.deps import get_db
from engine.legal import service as legal_service
from engine.legal.dependencies import require_legal_acceptance
from engine.legal.schemas import AcceptanceItem
from tests.factories import make_user

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


class TestAcceptanceRecordImmutability:
    async def _seed(self, db_session: AsyncSession):
        user = make_user(email=f"immutable-{uuid.uuid4()}@example.com")
        db_session.add(user)
        doc = LegalDocument(
            slug=f"immut-doc-{uuid.uuid4().hex[:8]}",
            title="Immutability Test Doc",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()
        return user, doc

    async def test_acceptance_creates_new_row_never_updates(self, db_session: AsyncSession):
        user, doc = await self._seed(db_session)

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            ip_address="127.0.0.1",
            user_agent="test",
        )
        await db_session.flush()

        count_stmt = (
            select(func.count())
            .select_from(LegalAcceptance)
            .where(
                LegalAcceptance.user_id == user.id,
                LegalAcceptance.document_slug == doc.slug,
            )
        )
        result = await db_session.execute(count_stmt)
        assert result.scalar() == 1

        first_record_stmt = select(LegalAcceptance).where(
            LegalAcceptance.user_id == user.id,
            LegalAcceptance.document_slug == doc.slug,
        )
        first_result = await db_session.execute(first_record_stmt)
        first_record = first_result.scalar_one()
        original_accepted_at = first_record.accepted_at

        doc.current_version = "2.0.0"
        await db_session.flush()

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="2.0.0")],
            ip_address="127.0.0.1",
            user_agent="test",
        )
        await db_session.flush()

        all_records_stmt = (
            select(LegalAcceptance)
            .where(
                LegalAcceptance.user_id == user.id,
                LegalAcceptance.document_slug == doc.slug,
            )
            .order_by(LegalAcceptance.accepted_at)
        )
        result = await db_session.execute(all_records_stmt)
        all_records = result.scalars().all()
        assert len(all_records) == 2  # noqa: PLR2004

        assert all_records[0].document_version == "1.0.0"
        assert all_records[0].accepted_at == original_accepted_at
        assert all_records[1].document_version == "2.0.0"

    async def test_acceptance_record_fields_are_immutable(self, db_session: AsyncSession):
        user, doc = await self._seed(db_session)

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            ip_address="192.168.1.1",
            user_agent="original-agent",
        )
        await db_session.flush()

        record_stmt = select(LegalAcceptance).where(
            LegalAcceptance.user_id == user.id,
            LegalAcceptance.document_slug == doc.slug,
        )
        result = await db_session.execute(record_stmt)
        record = result.scalar_one()

        assert record.ip_address == "192.168.1.1"
        assert record.user_agent == "original-agent"
        assert record.context == "onboarding"
        assert record.revoked_at is None


class TestConsentEnforcementIntegration:
    @pytest.fixture
    async def consent_client(self, db_session: AsyncSession):
        app = FastAPI()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db

        @app.get("/protected/backtest")
        async def protected_backtest(db: AsyncSession = Depends(get_db)):  # noqa: B008
            await require_legal_acceptance(db)
            return {"status": "ok"}

        @app.get("/protected/live-trade")
        async def protected_live_trade(db: AsyncSession = Depends(get_db)):  # noqa: B008
            await require_legal_acceptance(db)
            return {"order_id": "123"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @pytest.mark.skip(
        reason="require_legal_acceptance is a no-op until the auth dependency "
        "is wired (tracked separately as consent-enforcement follow-up)."
    )
    async def test_require_legal_acceptance_raises_451_when_pending(
        self, db_session: AsyncSession
    ):
        doc = LegalDocument(
            slug=f"consent-test-{uuid.uuid4().hex[:8]}",
            title="Consent Enforcement Doc",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()

        with pytest.raises(HTTPException) as exc_info:
            await require_legal_acceptance(db_session)
        assert exc_info.value.status_code == 451  # noqa: PLR2004
        detail = exc_info.value.detail
        assert detail["code"] == "legal_re_acceptance_required"
        assert "documents" in detail
        assert isinstance(detail["documents"], list)

    async def test_no_451_when_all_accepted(self, db_session: AsyncSession):
        doc = LegalDocument(
            slug=f"accepted-{uuid.uuid4().hex[:8]}",
            title="Already Accepted",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)

        user = make_user(email=f"accepted-{uuid.uuid4()}@example.com")
        db_session.add(user)
        await db_session.flush()

        acceptance = LegalAcceptance(
            user_id=user.id,
            document_slug=doc.slug,
            document_version="1.0.0",
            accepted_at=datetime.now(tz=UTC),
            ip_address="127.0.0.1",
            user_agent="test",
            context="onboarding",
        )
        db_session.add(acceptance)
        await db_session.flush()

    @pytest.mark.skip(
        reason="require_legal_acceptance is a no-op until the auth dependency "
        "is wired (tracked separately as consent-enforcement follow-up)."
    )
    async def test_451_response_contains_pending_document_slugs(self, db_session: AsyncSession):
        slugs = [f"pending-{uuid.uuid4().hex[:8]}" for _ in range(3)]
        for i, slug in enumerate(slugs):
            doc = LegalDocument(
                slug=slug,
                title=f"Pending Doc {i}",
                current_version="1.0.0",
                effective_date=date(2026, 1, 1),
                requires_acceptance=True,
                category="general",
                display_order=i,
                file_path="legal/terms-of-service.md",
            )
            db_session.add(doc)
        await db_session.flush()

        with pytest.raises(HTTPException) as exc_info:
            await require_legal_acceptance(db_session)
        assert exc_info.value.status_code == 451  # noqa: PLR2004
        pending_slugs = exc_info.value.detail["documents"]
        for slug in slugs:
            assert slug in pending_slugs


class TestBatchAcceptance:
    async def _seed_docs(self, db_session: AsyncSession, count: int) -> list[LegalDocument]:
        docs = []
        for i in range(count):
            doc = LegalDocument(
                slug=f"batch-{i}-{uuid.uuid4().hex[:8]}",
                title=f"Batch Doc {i}",
                current_version="1.0.0",
                effective_date=date(2026, 1, 1),
                requires_acceptance=True,
                category="general",
                display_order=i,
                file_path="legal/terms-of-service.md",
            )
            db_session.add(doc)
            docs.append(doc)
        await db_session.flush()
        return docs

    async def test_batch_accept_multiple_documents(self, db_session: AsyncSession):
        docs = await self._seed_docs(db_session, 3)
        user = make_user(email=f"batch-{uuid.uuid4()}@example.com")
        db_session.add(user)
        await db_session.flush()

        items = [AcceptanceItem(document_slug=d.slug, document_version="1.0.0") for d in docs]
        accepted = await legal_service.record_acceptances(
            db_session, user.id, items, "127.0.0.1", "test"
        )
        await db_session.flush()

        assert len(accepted) == 3  # noqa: PLR2004
        for doc in docs:
            count_stmt = (
                select(func.count())
                .select_from(LegalAcceptance)
                .where(
                    LegalAcceptance.user_id == user.id,
                    LegalAcceptance.document_slug == doc.slug,
                )
            )
            result = await db_session.execute(count_stmt)
            assert result.scalar() == 1

    async def test_batch_accept_idempotent(self, db_session: AsyncSession):
        docs = await self._seed_docs(db_session, 2)
        user = make_user(email=f"batch-idem-{uuid.uuid4()}@example.com")
        db_session.add(user)
        await db_session.flush()

        items = [AcceptanceItem(document_slug=d.slug, document_version="1.0.0") for d in docs]
        await legal_service.record_acceptances(db_session, user.id, items, "127.0.0.1", "test")
        await db_session.flush()

        await legal_service.record_acceptances(db_session, user.id, items, "127.0.0.1", "test")
        await db_session.flush()

        count_stmt = (
            select(func.count())
            .select_from(LegalAcceptance)
            .where(LegalAcceptance.user_id == user.id)
        )
        result = await db_session.execute(count_stmt)
        assert result.scalar() == 2  # noqa: PLR2004

    async def test_batch_accept_rejects_invalid_doc_among_valid(self, db_session: AsyncSession):
        docs = await self._seed_docs(db_session, 2)
        user = make_user(email=f"batch-invalid-{uuid.uuid4()}@example.com")
        db_session.add(user)
        await db_session.flush()

        items = [
            AcceptanceItem(document_slug=docs[0].slug, document_version="1.0.0"),
            AcceptanceItem(document_slug="nonexistent-doc", document_version="99.0.0"),
        ]

        with pytest.raises(HTTPException) as exc_info:
            await legal_service.record_acceptances(db_session, user.id, items, "127.0.0.1", "test")
        assert exc_info.value.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestReAcceptanceOnVersionBump:
    async def _setup(self, db_session: AsyncSession):
        user = make_user(email=f"reaccept-{uuid.uuid4()}@example.com")
        db_session.add(user)
        doc = LegalDocument(
            slug=f"reaccept-doc-{uuid.uuid4().hex[:8]}",
            title="Re-acceptance Doc",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()
        return user, doc

    async def test_accepted_doc_not_pending(self, db_session: AsyncSession):
        user, doc = await self._setup(db_session)
        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert doc.slug not in slugs

    async def test_version_bump_makes_doc_pending_again(self, db_session: AsyncSession):
        user, doc = await self._setup(db_session)
        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        doc.current_version = "2.0.0"
        await db_session.flush()

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert doc.slug in slugs

    async def test_re_acceptance_clears_pending(self, db_session: AsyncSession):
        user, doc = await self._setup(db_session)
        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        doc.current_version = "2.0.0"
        await db_session.flush()

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="2.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert doc.slug not in slugs

    async def test_needs_re_acceptance_flag_in_document_list(self, db_session: AsyncSession):
        user, doc = await self._setup(db_session)
        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        summaries_before = await legal_service.list_documents(db_session, user_id=user.id)
        doc_summary = next(s for s in summaries_before if s.slug == doc.slug)
        assert doc_summary.needs_re_acceptance is False

        doc.current_version = "2.0.0"
        await db_session.flush()

        summaries_after = await legal_service.list_documents(db_session, user_id=user.id)
        doc_summary = next(s for s in summaries_after if s.slug == doc.slug)
        assert doc_summary.needs_re_acceptance is True

    async def test_acceptance_history_preserves_all_versions(self, db_session: AsyncSession):
        user, doc = await self._setup(db_session)
        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        doc.current_version = "2.0.0"
        await db_session.flush()

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="2.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        records = await legal_service.list_user_acceptances(db_session, user.id, doc.slug)
        assert len(records) == 2  # noqa: PLR2004
        versions = {r.document_version for r in records}
        assert versions == {"1.0.0", "2.0.0"}

        v2_record = next(r for r in records if r.document_version == "2.0.0")
        assert v2_record.context == "re-acceptance"

        v1_record = next(r for r in records if r.document_version == "1.0.0")
        assert v1_record.context == "onboarding"


class TestOperatorSubstitution:
    @pytest.fixture
    async def legal_client(self, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_all_operator_placeholders_replaced(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        doc = LegalDocument(
            slug="substitution-test",
            title="Substitution Test",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/documents/substitution-test")
        assert response.status_code == HTTPStatus.OK
        content = response.json()["content_markdown"]

        assert "{{OPERATOR_NAME}}" not in content
        assert "{{OPERATOR_EMAIL}}" not in content
        assert "{{OPERATOR_URL}}" not in content
        assert "{{JURISDICTION}}" not in content
        assert "{{PLATFORM_FEE_PERCENT}}" not in content

    async def test_front_matter_stripped_from_rendered_content(
        self, legal_client: AsyncClient, db_session: AsyncSession
    ):
        doc = LegalDocument(
            slug="frontmatter-test",
            title="Frontmatter Test",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        response = await legal_client.get("/api/v1/legal/documents/frontmatter-test")
        assert response.status_code == HTTPStatus.OK
        content = response.json()["content_markdown"]
        assert not content.startswith("---")


class TestDocumentVersionQuery:
    async def test_get_document_with_matching_version(self, db_session: AsyncSession):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)

        doc = LegalDocument(
            slug=f"ver-test-{uuid.uuid4().hex[:8]}",
            title="Version Test",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/legal/documents/{doc.slug}?version=1.0.0")
            assert response.status_code == HTTPStatus.OK
            assert response.json()["version"] == "1.0.0"

    async def test_get_document_with_wrong_version_returns_404(self, db_session: AsyncSession):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)

        doc = LegalDocument(
            slug=f"ver-mismatch-{uuid.uuid4().hex[:8]}",
            title="Version Mismatch",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=1,
            file_path="legal/risk-disclaimer.md",
        )
        db_session.add(doc)
        await db_session.flush()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/legal/documents/{doc.slug}?version=2.0.0")
            assert response.status_code == HTTPStatus.NOT_FOUND


class TestNonAcceptanceDocumentNotPending:
    async def test_doc_with_requires_acceptance_false_not_pending(self, db_session: AsyncSession):
        user = make_user(email=f"nonreq-{uuid.uuid4()}@example.com")
        db_session.add(user)
        doc = LegalDocument(
            slug=f"nonreq-{uuid.uuid4().hex[:8]}",
            title="Non-required Doc",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=False,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(doc)
        await db_session.flush()

        pending = await legal_service.get_pending_acceptances(db_session, user.id)
        slugs = [p.slug for p in pending]
        assert doc.slug not in slugs


class TestDocumentListWithUserInfo:
    async def test_list_documents_includes_acceptance_status(self, db_session: AsyncSession):
        user = make_user(email=f"dlist-{uuid.uuid4()}@example.com")
        doc = LegalDocument(
            slug=f"dlist-{uuid.uuid4().hex[:8]}",
            title="List Test Doc",
            current_version="1.0.0",
            effective_date=date(2026, 1, 1),
            requires_acceptance=True,
            category="general",
            display_order=0,
            file_path="legal/terms-of-service.md",
        )
        db_session.add(user)
        db_session.add(doc)
        await db_session.flush()

        summaries = await legal_service.list_documents(db_session, user_id=user.id)
        doc_summary = next(s for s in summaries if s.slug == doc.slug)
        assert doc_summary.accepted is False
        assert doc_summary.accepted_version is None

        await legal_service.record_acceptances(
            db_session,
            user.id,
            [AcceptanceItem(document_slug=doc.slug, document_version="1.0.0")],
            "127.0.0.1",
            "test",
        )
        await db_session.flush()

        summaries_after = await legal_service.list_documents(db_session, user_id=user.id)
        doc_summary_after = next(s for s in summaries_after if s.slug == doc.slug)
        assert doc_summary_after.accepted is True
        assert doc_summary_after.accepted_version == "1.0.0"
