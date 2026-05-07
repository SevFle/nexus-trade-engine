"""Tests for engine.privacy.export — collect_user_data integration with DB session."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from engine.db.models import ApiKey, BacktestResult, DSRequest, LegalAcceptance, Portfolio, User, WebhookConfig
from engine.privacy.export import _jsonify, _row_to_dict, collect_user_data


@pytest.fixture
async def user_with_data(db_session):
    uid = uuid.uuid4()
    user = User(
        id=uid,
        email="export-test@example.com",
        display_name="Export Tester",
        is_active=True,
        role="user",
        auth_provider="local",
    )
    db_session.add(user)
    await db_session.flush()

    portfolio = Portfolio(
        id=uuid.uuid4(),
        user_id=uid,
        name="Test Portfolio",
        description="For export test",
    )
    db_session.add(portfolio)

    api_key = ApiKey(
        id=uuid.uuid4(),
        user_id=uid,
        name="test-key",
        prefix="test-",
        key_hash="hash123",
        scopes='["read"]',
    )
    db_session.add(api_key)

    webhook = WebhookConfig(
        id=uuid.uuid4(),
        user_id=uid,
        url="https://example.com/hook",
        event_types='["trade"]',
        signing_secret="secret123",
    )
    db_session.add(webhook)

    await db_session.flush()
    return uid


class TestCollectUserData:
    async def test_collect_user_data_returns_full_export(self, db_session, user_with_data):
        result = await collect_user_data(db_session, user_with_data)

        assert result["schema_version"] == 1
        assert result["user_id"] == str(user_with_data)
        assert result["user"]["email"] == "export-test@example.com"
        assert "password_hash" not in result["user"]
        assert len(result["portfolios"]) >= 1
        assert len(result["api_keys"]) >= 1
        assert "key_hash" not in result["api_keys"][0]
        assert len(result["webhooks"]) >= 1
        assert "signing_secret" not in result["webhooks"][0]

    async def test_collect_user_data_nonexistent_user_raises(self, db_session):
        fake_uid = uuid.uuid4()
        with pytest.raises(LookupError, match="user not found"):
            await collect_user_data(db_session, fake_uid)

    async def test_collect_user_data_empty_related_data(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="empty@example.com",
            display_name="Empty User",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)

        assert result["portfolios"] == []
        assert result["backtests"] == []
        assert result["webhooks"] == []
        assert result["api_keys"] == []
        assert result["dsr_history"] == []
        assert result["legal_acceptances"] == []

    async def test_exported_at_is_iso_format(self, db_session, user_with_data):
        result = await collect_user_data(db_session, user_with_data)
        assert "exported_at" in result
        assert "T" in result["exported_at"]

    async def test_pii_denylist_excludes_sensitive_fields(self, db_session, user_with_data):
        result = await collect_user_data(db_session, user_with_data)
        assert "password_hash" not in result["user"]
        assert "mfa_secret_encrypted" not in result["user"]
        assert "mfa_backup_codes" not in result["user"]


class TestGDPRBacktestExport:
    """Verify the outerjoin / OR / distinct fix for GDPR backtest queries (gh#157)."""

    async def test_backtests_linked_to_user_portfolio_are_included(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="bt-linked@example.com",
            display_name="BT Linked",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        portfolio = Portfolio(
            id=uuid.uuid4(),
            user_id=uid,
            name="BT Portfolio",
            description="",
        )
        db_session.add(portfolio)
        await db_session.flush()

        bt = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=portfolio.id,
            strategy_name="momentum",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, tzinfo=UTC),
            metrics={"sharpe": 1.2},
        )
        db_session.add(bt)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        bt_ids = [b["id"] for b in result["backtests"]]
        assert str(bt.id) in bt_ids

    async def test_orphaned_backtests_without_portfolio_are_excluded(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="bt-orphan@example.com",
            display_name="BT Orphan",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        orphaned_bt = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=None,
            strategy_name="orphan_strategy",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, tzinfo=UTC),
            metrics={},
        )
        db_session.add(orphaned_bt)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        bt_ids = [b["id"] for b in result["backtests"]]
        assert str(orphaned_bt.id) not in bt_ids

    async def test_orphaned_backtest_does_not_leak_to_other_user(self, db_session):
        uid_a = uuid.uuid4()
        uid_b = uuid.uuid4()
        user_a = User(
            id=uid_a,
            email="leak-a@example.com",
            display_name="Leak A",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        user_b = User(
            id=uid_b,
            email="leak-b@example.com",
            display_name="Leak B",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add_all([user_a, user_b])
        await db_session.flush()

        orphaned_bt = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=None,
            strategy_name="leaky_orphan",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, tzinfo=UTC),
            metrics={"note": "should not appear in any user export"},
        )
        db_session.add(orphaned_bt)
        await db_session.flush()

        result_a = await collect_user_data(db_session, uid_a)
        bt_ids_a = [b["id"] for b in result_a["backtests"]]
        assert str(orphaned_bt.id) not in bt_ids_a

        result_b = await collect_user_data(db_session, uid_b)
        bt_ids_b = [b["id"] for b in result_b["backtests"]]
        assert str(orphaned_bt.id) not in bt_ids_b

    async def test_other_user_portfolio_backtests_excluded(self, db_session):
        uid_a = uuid.uuid4()
        uid_b = uuid.uuid4()
        user_a = User(
            id=uid_a,
            email="user-a@example.com",
            display_name="User A",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        user_b = User(
            id=uid_b,
            email="user-b@example.com",
            display_name="User B",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add_all([user_a, user_b])
        await db_session.flush()

        portfolio_b = Portfolio(
            id=uuid.uuid4(),
            user_id=uid_b,
            name="B Portfolio",
            description="",
        )
        db_session.add(portfolio_b)
        await db_session.flush()

        bt_b = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=portfolio_b.id,
            strategy_name="secret_b",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, tzinfo=UTC),
            metrics={},
        )
        db_session.add(bt_b)
        await db_session.flush()

        result = await collect_user_data(db_session, uid_a)
        bt_ids = [b["id"] for b in result["backtests"]]
        assert str(bt_b.id) not in bt_ids

    async def test_no_duplicate_backtests(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="bt-dup@example.com",
            display_name="BT Dup",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        portfolio = Portfolio(
            id=uuid.uuid4(),
            user_id=uid,
            name="Dup Portfolio",
            description="",
        )
        db_session.add(portfolio)
        await db_session.flush()

        bt = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=portfolio.id,
            strategy_name="unique_strategy",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, tzinfo=UTC),
            metrics={},
        )
        db_session.add(bt)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        bt_ids = [b["id"] for b in result["backtests"]]
        assert bt_ids.count(str(bt.id)) == 1
