"""Comprehensive async tests for engine.privacy.deletion — request_deletion,
cancel_deletion, is_pending_deletion, is_due_for_purge, remaining_grace."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.db.models import User
from engine.privacy.deletion import (
    DeletionError,
    cancel_deletion,
    is_due_for_purge,
    is_pending_deletion,
    remaining_grace,
    request_deletion,
)


@pytest.fixture
async def test_user(db_session: AsyncSession):
    user = User(
        email=f"del_test_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Deletion Test User",
        is_active=True,
        role="user",
    )
    db_session.add(user)
    await db_session.flush()
    return user


class TestRequestDeletion:
    async def test_creates_pending_deletion(self, db_session: AsyncSession, test_user):
        row = await request_deletion(db_session, user_id=test_user.id)
        assert row.kind == "delete"
        assert row.status == "pending"
        assert row.details.get("grace_days") == 30

    async def test_with_note(self, db_session: AsyncSession, test_user):
        row = await request_deletion(
            db_session, user_id=test_user.id, note="Account closure requested"
        )
        assert row.note == "Account closure requested"

    async def test_rejects_duplicate_active_deletion(self, db_session: AsyncSession, test_user):
        await request_deletion(db_session, user_id=test_user.id)
        with pytest.raises(DeletionError, match="already has an active deletion"):
            await request_deletion(db_session, user_id=test_user.id)

    async def test_allows_new_deletion_after_cancel(self, db_session: AsyncSession, test_user):
        row = await request_deletion(db_session, user_id=test_user.id)
        await cancel_deletion(db_session, user_id=test_user.id)

        row2 = await request_deletion(db_session, user_id=test_user.id)
        assert row2.status == "pending"
        assert row2.id != row.id


class TestCancelDeletion:
    async def test_cancels_active_deletion(self, db_session: AsyncSession, test_user):
        await request_deletion(db_session, user_id=test_user.id)
        cancelled = await cancel_deletion(db_session, user_id=test_user.id)
        assert cancelled.status == "cancelled"
        assert cancelled.cancelled_at is not None

    async def test_raises_when_no_active_deletion(self, db_session: AsyncSession, test_user):
        with pytest.raises(DeletionError, match="no active deletion request"):
            await cancel_deletion(db_session, user_id=test_user.id)


class TestIsPendingDeletion:
    async def test_false_when_no_deletion(self, db_session: AsyncSession, test_user):
        pending, due_at = await is_pending_deletion(db_session, test_user.id)
        assert pending is False
        assert due_at is None

    async def test_true_when_pending(self, db_session: AsyncSession, test_user):
        row = await request_deletion(db_session, user_id=test_user.id)
        pending, due_at = await is_pending_deletion(db_session, test_user.id)
        assert pending is True
        assert due_at == row.sla_due_at

    async def test_false_after_cancel(self, db_session: AsyncSession, test_user):
        await request_deletion(db_session, user_id=test_user.id)
        await cancel_deletion(db_session, user_id=test_user.id)
        pending, due_at = await is_pending_deletion(db_session, test_user.id)
        assert pending is False


class TestIsDueForPurge:
    async def test_false_when_no_deletion(self, db_session: AsyncSession, test_user):
        assert await is_due_for_purge(db_session, test_user.id) is False

    async def test_false_when_within_grace(self, db_session: AsyncSession, test_user):
        row = await request_deletion(db_session, user_id=test_user.id)
        row.sla_due_at = datetime.now(tz=UTC) + timedelta(days=30)
        await db_session.flush()
        assert await is_due_for_purge(db_session, test_user.id) is False

    async def test_true_when_past_due(self, db_session: AsyncSession, test_user):
        row = await request_deletion(db_session, user_id=test_user.id)
        row.sla_due_at = datetime.now(tz=UTC) - timedelta(days=1)
        await db_session.flush()
        assert await is_due_for_purge(db_session, test_user.id) is True


class TestRemainingGrace:
    def test_positive_remaining(self):
        now = datetime(2026, 5, 5, tzinfo=UTC)
        due = now + timedelta(days=15)
        assert remaining_grace(now, due) == timedelta(days=15)

    def test_zero_when_expired(self):
        now = datetime(2026, 5, 5, tzinfo=UTC)
        due = now - timedelta(seconds=1)
        assert remaining_grace(now, due) == timedelta(0)

    def test_zero_at_exact_due(self):
        now = datetime(2026, 5, 5, tzinfo=UTC)
        assert remaining_grace(now, now) == timedelta(0)

    def test_small_positive_remaining(self):
        now = datetime(2026, 5, 5, tzinfo=UTC)
        due = now + timedelta(seconds=1)
        grace = remaining_grace(now, due)
        assert grace.total_seconds() > 0
