"""Tests for the post-grace purge path of engine.privacy.deletion.

These cover the scheduling + anonymization code added in gh#157 / #648:
``schedule_deletion``, ``anonymize_user``, ``list_due_schedules`` and
``process_due_deletions`` — plus the new account-disable / schedule-create
side effects of ``request_deletion`` / ``cancel_deletion`` that the
async-lifecycle suite (test_deletion_async.py) does not assert.

The purge must be audit-chain preserving: the user row is tombstoned
(PII scrubbed) rather than hard-deleted so referentially-protected
legal/audit rows survive, while owned domain data is deleted.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.db.models import (
    ApiKey,
    BacktestResult,
    ConsentRecord,
    DeletionSchedule,
    DSRequest,
    Portfolio,
    RefreshToken,
    User,
    WebhookConfig,
)
from engine.privacy.deletion import (
    DEFAULT_RETENTION_EXCEPTIONS,
    AnonymizationResult,
    anonymize_user,
    cancel_deletion,
    list_due_schedules,
    process_due_deletions,
    request_deletion,
    schedule_deletion,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(*, active: bool = True, restricted: bool = False, email: str | None = None) -> User:
    return User(
        email=email or f"purge_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Purge Target",
        hashed_password="$2b$12$oldhashplaceholder",
        is_active=active,
        role="user",
        auth_provider="local",
        # Unique per user so the (auth_provider, external_id) unique index
        # holds when several users coexist in one session.
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        mfa_enabled=True,
        mfa_secret_encrypted="enc-secret",
        mfa_backup_codes=["code1", "code2"],
        processing_restricted=restricted,
    )


async def _seed_owned_data(session: AsyncSession, user: User) -> dict[str, list]:
    """Create a portfolio (+backtest) and the other owned-domain rows."""
    portfolio = Portfolio(
        user_id=user.id,
        name="Main",
        initial_capital=100000,
    )
    session.add(portfolio)
    await session.flush()

    backtest = BacktestResult(
        portfolio_id=portfolio.id,
        strategy_name="momentum",
        start_date=datetime(2024, 1, 1, tzinfo=UTC),
        end_date=datetime(2024, 6, 1, tzinfo=UTC),
    )
    webhook = WebhookConfig(
        user_id=user.id,
        url="https://example.com/hook",
        event_types=["order.filled"],
        signing_secret="secret",
    )
    api_key = ApiKey(
        user_id=user.id,
        name="ci",
        prefix="nxs_" + uuid.uuid4().hex[:8],
        key_hash="hashed",
        scopes=["read"],
    )
    token = RefreshToken(
        user_id=user.id,
        token_hash=uuid.uuid4().hex,
        expires_at=datetime.now(tz=UTC) + timedelta(days=1),
    )
    consent = ConsentRecord(
        user_id=user.id, purpose="analytics", granted=True, source="settings"
    )
    session.add_all([backtest, webhook, api_key, token, consent])
    await session.flush()
    return {
        "portfolio": portfolio,
        "backtest": backtest,
        "webhook": webhook,
        "api_key": api_key,
        "token": token,
        "consent": consent,
    }


async def _make_delete_request(session: AsyncSession, user: User) -> DSRequest:
    """Insert a delete DSR row without going through request_deletion."""
    row = DSRequest(
        user_id=user.id,
        kind="delete",
        status="pending",
        details={"grace_days": 30},
        sla_due_at=datetime.now(tz=UTC) + timedelta(days=30),
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def user_with_data(db_session: AsyncSession):
    user = _make_user()
    db_session.add(user)
    await db_session.flush()
    await _seed_owned_data(db_session, user)
    return user


# ---------------------------------------------------------------------------
# schedule_deletion
# ---------------------------------------------------------------------------


class TestScheduleDeletion:
    async def test_creates_schedule_with_defaults(
        self, db_session: AsyncSession, user_with_data: User
    ):
        request = await _make_delete_request(db_session, user_with_data)
        schedule = await schedule_deletion(db_session, request=request)

        assert schedule.user_id == user_with_data.id
        assert schedule.dsr_request_id == request.id
        assert schedule.status == "scheduled"
        assert schedule.scheduled_for == request.sla_due_at
        assert schedule.retention_exceptions == DEFAULT_RETENTION_EXCEPTIONS

    async def test_custom_retention_exceptions(
        self, db_session: AsyncSession, user_with_data: User
    ):
        request = await _make_delete_request(db_session, user_with_data)
        custom = {"custom_table": "specific reason"}
        schedule = await schedule_deletion(
            db_session, request=request, retention_exceptions=custom
        )
        assert schedule.retention_exceptions == custom

    async def test_falsy_retention_falls_back_to_defaults(
        self, db_session: AsyncSession, user_with_data: User
    ):
        # ``schedule_deletion`` treats a falsy value (None / {}) as "not
        # provided" and applies DEFAULT_RETENTION_EXCEPTIONS. This pins
        # the ``dict(retention_exceptions or DEFAULTS)`` contract.
        request = await _make_delete_request(db_session, user_with_data)
        empty = await schedule_deletion(
            db_session, request=request, retention_exceptions={}
        )
        assert empty.retention_exceptions == DEFAULT_RETENTION_EXCEPTIONS

    async def test_is_idempotent(
        self, db_session: AsyncSession, user_with_data: User
    ):
        request = await _make_delete_request(db_session, user_with_data)
        first = await schedule_deletion(db_session, request=request)
        second = await schedule_deletion(db_session, request=request)
        assert second.id == first.id
        # No duplicate schedule rows were created.
        rows = (
            await db_session.execute(
                select(DeletionSchedule).where(
                    DeletionSchedule.dsr_request_id == request.id
                )
            )
        ).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# request_deletion / cancel_deletion new side effects
# ---------------------------------------------------------------------------


class TestRequestCancelSideEffects:
    async def test_request_disables_account_and_creates_schedule(
        self, db_session: AsyncSession, user_with_data: User
    ):
        assert user_with_data.is_active is True
        request = await request_deletion(db_session, user_id=user_with_data.id)

        await db_session.refresh(user_with_data)
        assert user_with_data.is_active is False

        schedule = (
            await db_session.execute(
                select(DeletionSchedule).where(
                    DeletionSchedule.dsr_request_id == request.id
                )
            )
        ).scalar_one()
        assert schedule.status == "scheduled"

    async def test_request_creates_schedule_referencing_dsr(
        self, db_session: AsyncSession, user_with_data: User
    ):
        # The schedule must point back at the DSR that opened the grace
        # window so the purge job can complete the audit chain.
        request = await request_deletion(db_session, user_id=user_with_data.id)
        schedule = await schedule_deletion(db_session, request=request)
        assert schedule.dsr_request_id == request.id
        assert schedule.user_id == user_with_data.id
        # SQLite strips tzinfo on read-back; compare the instant ignoring tz.
        assert schedule.scheduled_for.replace(tzinfo=None) == request.sla_due_at.replace(
            tzinfo=None
        )

    async def test_cancel_reactivates_account_and_cancels_schedule(
        self, db_session: AsyncSession, user_with_data: User
    ):
        request = await request_deletion(db_session, user_id=user_with_data.id)
        await db_session.refresh(user_with_data)
        assert user_with_data.is_active is False

        cancelled = await cancel_deletion(db_session, user_id=user_with_data.id)
        assert cancelled.status == "cancelled"

        await db_session.refresh(user_with_data)
        assert user_with_data.is_active is True

        schedule = (
            await db_session.execute(
                select(DeletionSchedule).where(
                    DeletionSchedule.dsr_request_id == request.id
                )
            )
        ).scalar_one()
        assert schedule.status == "cancelled"


# ---------------------------------------------------------------------------
# anonymize_user
# ---------------------------------------------------------------------------


class TestAnonymizeUser:
    async def test_raises_lookuperror_for_missing_user(self, db_session: AsyncSession):
        with pytest.raises(LookupError, match="user not found"):
            await anonymize_user(db_session, uuid.uuid4())

    async def test_returns_result_with_expected_shape(
        self, db_session: AsyncSession, user_with_data: User
    ):
        result = await anonymize_user(db_session, user_with_data.id)
        assert isinstance(result, AnonymizationResult)
        assert result.user_id == user_with_data.id
        assert result.dsr_request_id is None
        assert result.schedule_id is None
        assert result.retention_exceptions == DEFAULT_RETENTION_EXCEPTIONS

    async def test_anonymized_label_is_stable_hash(
        self, db_session: AsyncSession, user_with_data: User
    ):
        expected_hash = hashlib.sha256(str(user_with_data.id).encode()).hexdigest()[:16]
        result = await anonymize_user(db_session, user_with_data.id)
        assert result.anonymized_label == f"anonymized:{expected_hash}"

    async def test_user_row_is_tombstoned_not_deleted(
        self, db_session: AsyncSession, user_with_data: User
    ):
        original_id = user_with_data.id
        await anonymize_user(db_session, original_id)

        # Row still exists (kept for referential integrity).
        user = (
            await db_session.execute(select(User).where(User.id == original_id))
        ).scalar_one()
        expected_label = f"anonymized:{hashlib.sha256(str(original_id).encode()).hexdigest()[:16]}"
        assert user.email == f"{expected_label}@anonymized.local"
        assert user.hashed_password is None
        assert user.display_name == "Deleted User"
        assert user.mfa_enabled is False
        assert user.mfa_secret_encrypted is None
        assert user.mfa_backup_codes is None
        assert user.external_id is None
        assert user.is_active is False
        assert user.processing_restricted is False

    async def test_owned_domain_data_is_deleted(
        self, db_session: AsyncSession, user_with_data: User
    ):
        uid = user_with_data.id
        await anonymize_user(db_session, uid)

        assert (await db_session.execute(select(Portfolio).where(Portfolio.user_id == uid))).scalars().first() is None
        assert (await db_session.execute(select(WebhookConfig).where(WebhookConfig.user_id == uid))).scalars().first() is None
        assert (await db_session.execute(select(ApiKey).where(ApiKey.user_id == uid))).scalars().first() is None
        assert (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == uid))).scalars().first() is None
        assert (await db_session.execute(select(ConsentRecord).where(ConsentRecord.user_id == uid))).scalars().first() is None
        assert (await db_session.execute(select(BacktestResult))).scalars().first() is None

    async def test_purge_counts_recorded(
        self, db_session: AsyncSession, user_with_data: User
    ):
        result = await anonymize_user(db_session, user_with_data.id)
        # One portfolio, one webhook, one api key, one token, one consent.
        assert result.purged["portfolios"] == 1
        assert result.purged["webhooks"] == 1
        assert result.purged["api_keys"] == 1
        assert result.purged["refresh_tokens"] == 1
        assert result.purged["consents"] == 1

    async def test_no_portfolios_yields_no_portfolios_key(
        self, db_session: AsyncSession
    ):
        user = _make_user()
        db_session.add(user)
        await db_session.flush()
        # Seed only a webhook so some data is present but no portfolios.
        db_session.add(
            WebhookConfig(
                user_id=user.id,
                url="https://example.com/h",
                event_types=["x"],
                signing_secret="s",
            )
        )
        await db_session.flush()

        result = await anonymize_user(db_session, user.id)
        assert "portfolios" not in result.purged
        assert result.purged["webhooks"] == 1

    async def test_with_dsr_request_completes_request_and_schedule(
        self, db_session: AsyncSession, user_with_data: User
    ):
        request = await request_deletion(db_session, user_id=user_with_data.id)
        # Account is disabled by request_deletion; anonymize then purges.
        result = await anonymize_user(
            db_session, user_with_data.id, dsr_request_id=request.id
        )

        assert result.dsr_request_id == request.id
        assert result.schedule_id is not None

        await db_session.refresh(request)
        assert request.status == "completed"
        assert request.completed_at is not None

        schedule = (
            await db_session.execute(
                select(DeletionSchedule).where(DeletionSchedule.id == result.schedule_id)
            )
        ).scalar_one()
        assert schedule.status == "purged"
        assert schedule.purged_at is not None
        assert schedule.anonymized_label == result.anonymized_label
        assert schedule.retention_exceptions == DEFAULT_RETENTION_EXCEPTIONS

    async def test_with_unknown_dsr_request_id_is_graceful(
        self, db_session: AsyncSession, user_with_data: User
    ):
        # A schedule exists for a *different* request, so the lookup misses.
        request = await _make_delete_request(db_session, user_with_data)
        await schedule_deletion(db_session, request=request)

        bogus = uuid.uuid4()
        result = await anonymize_user(
            db_session, user_with_data.id, dsr_request_id=bogus
        )
        # No request/schedule matched -> no schedule_id, no crash, user purged.
        assert result.dsr_request_id == bogus
        assert result.schedule_id is None
        # Original schedule is untouched.
        await db_session.refresh(request)
        assert request.status == "pending"


# ---------------------------------------------------------------------------
# list_due_schedules
# ---------------------------------------------------------------------------


class TestListDueSchedules:
    async def _add_schedule(
        self,
        session: AsyncSession,
        user: User,
        *,
        scheduled_for: datetime,
        status: str = "scheduled",
    ) -> DeletionSchedule:
        request = await _make_delete_request(session, user)
        schedule = DeletionSchedule(
            user_id=user.id,
            dsr_request_id=request.id,
            scheduled_for=scheduled_for,
            status=status,
            retention_exceptions=dict(DEFAULT_RETENTION_EXCEPTIONS),
        )
        session.add(schedule)
        await session.flush()
        return schedule

    async def test_returns_only_past_due_scheduled(
        self, db_session: AsyncSession
    ):
        now = datetime.now(tz=UTC)
        u_past = _make_user(email="past@example.com")
        u_future = _make_user(email="future@example.com")
        u_purged = _make_user(email="purged@example.com")
        db_session.add_all([u_past, u_future, u_purged])
        await db_session.flush()

        past = await self._add_schedule(db_session, u_past, scheduled_for=now - timedelta(days=1))
        await self._add_schedule(db_session, u_future, scheduled_for=now + timedelta(days=5))
        await self._add_schedule(
            db_session, u_purged, scheduled_for=now - timedelta(days=2), status="purged"
        )

        due = await list_due_schedules(db_session, now=now)
        due_ids = [s.id for s in due]
        assert past.id in due_ids
        assert all(s.status == "scheduled" for s in due)
        assert len(due) == 1

    async def test_orders_by_scheduled_for(self, db_session: AsyncSession):
        now = datetime.now(tz=UTC)
        users = [_make_user(email=f"u{i}@example.com") for i in range(3)]
        db_session.add_all(users)
        await db_session.flush()
        await self._add_schedule(db_session, users[2], scheduled_for=now - timedelta(hours=1))
        await self._add_schedule(db_session, users[0], scheduled_for=now - timedelta(days=3))
        await self._add_schedule(db_session, users[1], scheduled_for=now - timedelta(days=2))

        due = await list_due_schedules(db_session, now=now)
        assert len(due) == 3
        assert due == sorted(due, key=lambda s: s.scheduled_for)

    async def test_uses_now_when_omitted(self, db_session: AsyncSession):
        user = _make_user(email="n1@example.com")
        db_session.add(user)
        await db_session.flush()
        await self._add_schedule(
            db_session, user, scheduled_for=datetime.now(tz=UTC) - timedelta(days=10)
        )
        due = await list_due_schedules(db_session)
        assert len(due) == 1

    async def test_empty_when_nothing_due(self, db_session: AsyncSession):
        assert await list_due_schedules(db_session) == []


# ---------------------------------------------------------------------------
# process_due_deletions
# ---------------------------------------------------------------------------


class TestProcessDueDeletions:
    async def test_purges_each_due_schedule(self, db_session: AsyncSession):
        user = _make_user(email="proc@example.com")
        db_session.add(user)
        await db_session.flush()
        request = await request_deletion(db_session, user_id=user.id)
        # Force the schedule into the past.
        schedule = await schedule_deletion(db_session, request=request)
        schedule.scheduled_for = datetime.now(tz=UTC) - timedelta(days=1)
        await db_session.flush()

        results = await process_due_deletions(db_session)
        assert len(results) == 1
        assert results[0].user_id == user.id
        assert results[0].schedule_id == schedule.id

        # anonymize_user stages the schedule update without an explicit
        # flush (the operator job commits the unit of work); flush here
        # to model that commit boundary before asserting persisted state.
        await db_session.flush()
        await db_session.refresh(schedule)
        assert schedule.status == "purged"
        await db_session.refresh(request)
        assert request.status == "completed"

    async def test_returns_empty_when_nothing_due(self, db_session: AsyncSession):
        assert await process_due_deletions(db_session) == []

    async def test_respects_explicit_now(self, db_session: AsyncSession):
        user = _make_user(email="proc2@example.com")
        db_session.add(user)
        await db_session.flush()
        request = await request_deletion(db_session, user_id=user.id)
        schedule = await schedule_deletion(db_session, request=request)
        # Schedule is 5 days in the future relative to wall clock.
        schedule.scheduled_for = datetime.now(tz=UTC) + timedelta(days=5)
        await db_session.flush()

        future_now = datetime.now(tz=UTC) + timedelta(days=10)
        results = await process_due_deletions(db_session, now=future_now)
        assert len(results) == 1
        await db_session.flush()
        await db_session.refresh(schedule)
        assert schedule.status == "purged"
