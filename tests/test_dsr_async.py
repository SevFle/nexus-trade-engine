"""Comprehensive async tests for engine.privacy.dsr — record_request,
list_user_requests, and transition using the test DB session."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.db.models import DSRequest, User
from engine.privacy.dsr import (
    DSR_KINDS,
    list_user_requests,
    record_request,
    transition,
)


@pytest.fixture
async def test_user(db_session: AsyncSession):
    user = User(
        email=f"dsr_test_{uuid.uuid4().hex[:8]}@example.com",
        display_name="DSR Test User",
        is_active=True,
        role="user",
    )
    db_session.add(user)
    await db_session.flush()
    return user


class TestRecordRequest:
    async def test_records_valid_kind(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        assert row.user_id == test_user.id
        assert row.kind == "export"
        assert row.status == "pending"
        assert row.sla_due_at is not None

    async def test_all_valid_kinds(self, db_session: AsyncSession, test_user):
        for kind in DSR_KINDS:
            row = await record_request(db_session, user_id=test_user.id, kind=kind)
            assert row.kind == kind

    async def test_rejects_invalid_kind(self, db_session: AsyncSession, test_user):
        with pytest.raises(ValueError, match="unknown DSR kind"):
            await record_request(db_session, user_id=test_user.id, kind="invalid")

    async def test_rejects_zero_sla_days(self, db_session: AsyncSession, test_user):
        with pytest.raises(ValueError, match="sla_days must be positive"):
            await record_request(db_session, user_id=test_user.id, kind="export", sla_days=0)

    async def test_rejects_negative_sla_days(self, db_session: AsyncSession, test_user):
        with pytest.raises(ValueError, match="sla_days must be positive"):
            await record_request(db_session, user_id=test_user.id, kind="export", sla_days=-5)

    async def test_custom_sla_days(self, db_session: AsyncSession, test_user):
        row = await record_request(
            db_session, user_id=test_user.id, kind="delete", sla_days=7
        )
        assert row.sla_due_at is not None

    async def test_with_note(self, db_session: AsyncSession, test_user):
        row = await record_request(
            db_session,
            user_id=test_user.id,
            kind="export",
            note="User requested export via support",
        )
        assert row.note == "User requested export via support"

    async def test_with_details(self, db_session: AsyncSession, test_user):
        details = {"format": "json", "scope": "all"}
        row = await record_request(
            db_session, user_id=test_user.id, kind="export", details=details
        )
        assert row.details == details

    async def test_default_details_empty_dict(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="rectify")
        assert row.details == {}


class TestListUserRequests:
    async def test_empty_for_new_user(self, db_session: AsyncSession, test_user):
        results = await list_user_requests(db_session, test_user.id)
        assert results == []

    async def test_returns_user_specific_requests(self, db_session: AsyncSession):
        user_a = User(
            email=f"dsr_a_{uuid.uuid4().hex[:8]}@example.com",
            display_name="User A",
        )
        user_b = User(
            email=f"dsr_b_{uuid.uuid4().hex[:8]}@example.com",
            display_name="User B",
        )
        db_session.add_all([user_a, user_b])
        await db_session.flush()

        await record_request(db_session, user_id=user_a.id, kind="export")
        await record_request(db_session, user_id=user_b.id, kind="delete")

        results_a = await list_user_requests(db_session, user_a.id)
        results_b = await list_user_requests(db_session, user_b.id)
        assert len(results_a) == 1
        assert results_a[0].kind == "export"
        assert len(results_b) == 1
        assert results_b[0].kind == "delete"

    async def test_ordered_by_created_at_desc(self, db_session: AsyncSession, test_user):
        r1 = await record_request(db_session, user_id=test_user.id, kind="export")
        r2 = await record_request(db_session, user_id=test_user.id, kind="delete")

        results = await list_user_requests(db_session, test_user.id)
        assert len(results) == 2
        assert results[0].id == r2.id
        assert results[1].id == r1.id


class TestTransition:
    async def test_to_in_progress(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        updated = await transition(db_session, row, status="in_progress")
        assert updated.status == "in_progress"

    async def test_to_completed_sets_completed_at(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        updated = await transition(db_session, row, status="completed")
        assert updated.status == "completed"
        assert updated.completed_at is not None

    async def test_to_cancelled_sets_cancelled_at(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        updated = await transition(db_session, row, status="cancelled")
        assert updated.status == "cancelled"
        assert updated.cancelled_at is not None

    async def test_to_failed(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        updated = await transition(db_session, row, status="failed")
        assert updated.status == "failed"

    async def test_idempotent_terminal_to_same(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        completed = await transition(db_session, row, status="completed")
        again = await transition(db_session, completed, status="completed")
        assert again.status == "completed"

    async def test_rejects_invalid_status(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        with pytest.raises(ValueError, match="unknown DSR status"):
            await transition(db_session, row, status="nonsense")

    async def test_full_lifecycle(self, db_session: AsyncSession, test_user):
        row = await record_request(db_session, user_id=test_user.id, kind="export")
        await transition(db_session, row, status="in_progress")
        assert row.status == "in_progress"

        await transition(db_session, row, status="completed")
        assert row.status == "completed"
        assert row.completed_at is not None
