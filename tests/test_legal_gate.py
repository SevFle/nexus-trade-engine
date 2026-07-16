"""Legal Gate acceptance-tracking backend slice.

Vertical-slice tests for the model, repository, and HTTP endpoints introduced
for the Legal Gate / Legal surfaces acceptance-tracking feature:

* :class:`engine.legal.models.LegalAcceptance` — SQLAlchemy audit row.
* :func:`engine.legal.repository.record_acceptance` /
  :func:`engine.legal.repository.get_latest_acceptance` — async data access.
* ``POST /api/legal/accept`` and ``GET /api/legal/status`` — routes registered
  on the legal API router.

Scope is intentionally tight: happy-path recording + retrieval only. No UI,
no document body storage. The model maps to its own ``legal_gate_acceptances``
table, so these tests are fully isolated from the document-management
``legal_acceptances`` table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import settings
from engine.legal.models import LegalAcceptance
from engine.legal.repository import get_latest_acceptance, record_acceptance

CURRENT_VERSION = settings.legal_terms_version
OTHER_VERSION = "0.9.0"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class TestLegalAcceptanceModel:
    def test_uses_isolated_table(self) -> None:
        # Must not collide with engine.db.models.LegalAcceptance
        # (table "legal_acceptances").
        assert LegalAcceptance.__tablename__ == "legal_gate_acceptances"

    def test_columns_match_slice_spec(self) -> None:
        cols = {c.name for c in LegalAcceptance.__table__.columns}
        # Primary key + the four acceptance-tracking facts from the spec.
        assert {"id", "user_id", "document_version", "accepted_at", "ip_address"} <= cols

    def test_supports_compound_indexes_for_common_queries(self) -> None:
        index_names = {idx.name for idx in LegalAcceptance.__table__.indexes}
        assert "ix_legal_gate_user_version" in index_names
        assert "ix_legal_gate_user_time" in index_names


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class TestRepository:
    async def test_record_acceptance_persists_and_returns_row(
        self, db_session: AsyncSession
    ) -> None:
        record = await record_acceptance(
            db_session, "user-1", CURRENT_VERSION, "10.0.0.1"
        )
        assert record.id is not None
        assert record.user_id == "user-1"
        assert record.document_version == CURRENT_VERSION
        assert record.ip_address == "10.0.0.1"

        latest = await get_latest_acceptance(db_session, "user-1")
        assert latest is not None
        assert latest.id == record.id

    async def test_record_acceptance_defaults_accepted_at_to_utc_now(
        self, db_session: AsyncSession
    ) -> None:
        before = datetime.now(tz=UTC)
        record = await record_acceptance(
            db_session, "user-2", CURRENT_VERSION, "127.0.0.1"
        )
        after = datetime.now(tz=UTC)
        assert before <= record.accepted_at <= after
        assert record.accepted_at.tzinfo is not None

    async def test_get_latest_acceptance_returns_none_when_absent(
        self, db_session: AsyncSession
    ) -> None:
        assert await get_latest_acceptance(db_session, "never-existed") is None

    async def test_get_latest_returns_most_recent_by_accepted_at(
        self, db_session: AsyncSession
    ) -> None:
        older = datetime.now(tz=UTC) - timedelta(hours=3)
        newer = datetime.now(tz=UTC) - timedelta(minutes=5)
        await record_acceptance(
            db_session, "user-3", OTHER_VERSION, "1.1.1.1", accepted_at=older
        )
        await record_acceptance(
            db_session, "user-3", CURRENT_VERSION, "1.1.1.2", accepted_at=newer
        )

        latest = await get_latest_acceptance(db_session, "user-3")
        assert latest is not None
        assert latest.document_version == CURRENT_VERSION
        assert latest.ip_address == "1.1.1.2"

    async def test_records_are_user_scoped(self, db_session: AsyncSession) -> None:
        await record_acceptance(db_session, "user-a", CURRENT_VERSION, "2.2.2.2")
        await record_acceptance(db_session, "user-b", CURRENT_VERSION, "3.3.3.3")

        latest_a = await get_latest_acceptance(db_session, "user-a")
        latest_b = await get_latest_acceptance(db_session, "user-b")
        assert latest_a is not None
        assert latest_b is not None
        assert latest_a.user_id == "user-a"
        assert latest_b.user_id == "user-b"

    async def test_record_acceptance_rejects_invalid_inputs(
        self, db_session: AsyncSession
    ) -> None:
        with pytest.raises(ValueError):
            await record_acceptance(db_session, "", CURRENT_VERSION, "1.2.3.4")
        with pytest.raises(ValueError):
            await record_acceptance(db_session, "user-x", "", "1.2.3.4")

    async def test_accepted_at_is_always_timezone_aware_after_round_trip(
        self, db_session: AsyncSession
    ) -> None:
        # Regression: SQLite strips tzinfo from DateTime(timezone=True) on
        # store, so both the freshly-recorded row and a subsequent lookup
        # must still surface a timezone-aware UTC datetime.
        record = await record_acceptance(
            db_session, "user-tz", CURRENT_VERSION, "9.9.9.9"
        )
        assert record.accepted_at.tzinfo is not None
        assert record.accepted_at.utcoffset() == timedelta(0)

        latest = await get_latest_acceptance(db_session, "user-tz")
        assert latest is not None
        assert latest.accepted_at.tzinfo is not None
        assert latest.accepted_at.utcoffset() == timedelta(0)

    async def test_record_acceptance_normalises_naive_and_non_utc_input(
        self, db_session: AsyncSession
    ) -> None:
        # A naive timestamp is treated as UTC; an explicitly non-UTC tz is
        # converted to UTC. Both must come back aware and at UTC offset.
        naive = datetime(2024, 1, 2, 3, 4, 5)
        rec_naive = await record_acceptance(
            db_session, "user-naive", CURRENT_VERSION, "1.1.1.1", accepted_at=naive
        )
        assert rec_naive.accepted_at.tzinfo is not None
        assert rec_naive.accepted_at.utcoffset() == timedelta(0)
        assert rec_naive.accepted_at.replace(tzinfo=None) == naive

        ahead = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=5)))
        rec_ahead = await record_acceptance(
            db_session, "user-ahead", CURRENT_VERSION, "1.1.1.1", accepted_at=ahead
        )
        assert rec_ahead.accepted_at.utcoffset() == timedelta(0)
        # 03:00+05:00 == 22:00Z previous day.
        assert rec_ahead.accepted_at == ahead.astimezone(UTC)


# --------------------------------------------------------------------------- #
# HTTP endpoints (POST /api/legal/accept, GET /api/legal/status)
# --------------------------------------------------------------------------- #
class TestLegalGateEndpoints:
    """Happy-path acceptance recording and retrieval via the registered routes.

    Uses the shared ``db_client`` fixture, which wires a real FastAPI app to an
    isolated per-test DB session with auth pre-bypassed to a fixed user.
    """

    async def test_accept_then_status_reports_accepted(self, db_client: AsyncClient) -> None:
        # 1. Record an acceptance for the current version.
        resp = await db_client.post(
            "/api/legal/accept", json={"document_version": CURRENT_VERSION}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        acceptance = body["acceptance"]
        assert acceptance["document_version"] == CURRENT_VERSION
        assert acceptance["ip_address"]  # captured from the request
        # accepted_at round-trips as a timezone-aware ISO timestamp.
        parsed = datetime.fromisoformat(acceptance["accepted_at"])
        assert parsed.tzinfo is not None

        # 2. Status reflects the freshly recorded acceptance.
        status = await db_client.get("/api/legal/status")
        assert status.status_code == 200, status.text
        sbody = status.json()
        assert sbody["accepted"] is True
        assert sbody["needs_acceptance"] is False
        assert sbody["current_version"] == CURRENT_VERSION
        assert sbody["accepted_version"] == CURRENT_VERSION
        # Regression: accepted_at round-trips via the status route as a
        # timezone-aware ISO timestamp even on the SQLite test backend,
        # which otherwise drops tzinfo on DateTime columns.
        status_parsed = datetime.fromisoformat(sbody["accepted_at"])
        assert status_parsed.tzinfo is not None
        assert status_parsed.utcoffset() == timedelta(0)

    async def test_status_reports_not_accepted_with_no_record(
        self, db_client: AsyncClient
    ) -> None:
        # Per-test rollback means no prior acceptance exists for this user.
        status = await db_client.get("/api/legal/status")
        assert status.status_code == 200, status.text
        sbody = status.json()
        assert sbody["accepted"] is False
        assert sbody["needs_acceptance"] is True
        assert sbody["accepted_version"] is None
        assert sbody["accepted_at"] is None
        assert sbody["current_version"] == CURRENT_VERSION

    async def test_accept_rejects_missing_version(self, db_client: AsyncClient) -> None:
        resp = await db_client.post("/api/legal/accept", json={"document_version": ""})
        assert resp.status_code == 422

    async def test_accept_rejects_unknown_fields(self, db_client: AsyncClient) -> None:
        resp = await db_client.post(
            "/api/legal/accept",
            json={"document_version": CURRENT_VERSION, "extra": "nope"},
        )
        assert resp.status_code == 422
