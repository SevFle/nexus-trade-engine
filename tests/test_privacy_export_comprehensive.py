"""Comprehensive tests for engine.privacy.export — collect_user_data edge cases,
_jsonify boundary values, _row_to_dict deny-list mechanics, and DSR module coverage.

Targets the most recently changed code: the backtest-join fix in collect_user_data (gh#157)
and the _jsonify / _row_to_dict helper functions.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.db.models import (
    ApiKey,
    BacktestResult,
    LegalAcceptance,
    Portfolio,
    User,
    WebhookConfig,
)
from engine.privacy.dsr import (
    DSR_KINDS,
    list_user_requests,
    record_request,
    transition,
)
from engine.privacy.export import _jsonify, _row_to_dict, collect_user_data


class _FakeColumn:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeTable:
    def __init__(self, columns: list[str]) -> None:
        self.columns = [_FakeColumn(c) for c in columns]


class _FakeRow:
    def __init__(self, **fields) -> None:
        self.__table__ = _FakeTable(list(fields.keys()))
        for k, v in fields.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# _jsonify — boundary values and edge cases
# ---------------------------------------------------------------------------


class TestJsonifyBoundaryValues:
    def test_none(self):
        assert _jsonify(None) is None

    def test_bool_not_confused_with_int(self):
        assert _jsonify(True) is True
        assert _jsonify(False) is False
        assert isinstance(_jsonify(True), bool)

    def test_zero_and_negative_int(self):
        assert _jsonify(0) == 0
        assert _jsonify(-1) == -1

    def test_float_precision(self):
        assert _jsonify(0.1 + 0.2) == 0.1 + 0.2

    def test_empty_string(self):
        assert _jsonify("") == ""

    def test_empty_list(self):
        assert _jsonify([]) == []

    def test_empty_tuple(self):
        assert _jsonify(()) == []

    def test_empty_dict(self):
        assert _jsonify({}) == {}

    def test_nested_list(self):
        assert _jsonify([[1, 2], [3]]) == [[1, 2], [3]]

    def test_nested_dict(self):
        assert _jsonify({"a": {"b": 1}}) == {"a": {"b": 1}}

    def test_list_with_mixed_types(self):
        result = _jsonify([None, True, 1, "x", [2], {"k": "v"}])
        assert result == [None, True, 1, "x", [2], {"k": "v"}]

    def test_dict_with_non_string_keys_converted(self):
        result = _jsonify({42: "answer", True: "yes"})
        assert result == {"42": "answer", "True": "yes"}

    def test_datetime_in_list(self):
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        result = _jsonify([dt])
        assert result[0].startswith("2026-01-01")

    def test_datetime_in_dict_value(self):
        dt = datetime(2026, 6, 15, 12, 30, tzinfo=UTC)
        result = _jsonify({"ts": dt})
        assert result["ts"].startswith("2026-06-15T12:30")

    def test_uuid_converted_to_str(self):
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert _jsonify(u) == "12345678-1234-5678-1234-567812345678"

    def test_decimal_converted_to_str(self):
        assert _jsonify(Decimal("3.14")) == "3.14"

    def test_large_int(self):
        assert _jsonify(10**18) == 10**18

    def test_negative_float(self):
        assert _jsonify(-0.5) == -0.5

    def test_tuple_converted_to_list(self):
        assert _jsonify((1, 2, 3)) == [1, 2, 3]

    def test_deeply_nested_structure(self):
        data = {"a": [{"b": [1, {"c": 2}]}]}
        assert _jsonify(data) == {"a": [{"b": [1, {"c": 2}]}]}

    def test_set_falls_back_to_str(self):
        result = _jsonify({1, 2})
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _row_to_dict — deny-list and type handling
# ---------------------------------------------------------------------------


class TestRowToDictEdgeCases:
    def test_none_values_preserved(self):
        row = _FakeRow(a=None, b=1)
        out = _row_to_dict(row)
        assert out["a"] is None
        assert out["b"] == 1

    def test_uuid_column_stringified(self):
        u = uuid.uuid4()
        row = _FakeRow(id=u, name="test")
        out = _row_to_dict(row)
        assert out["id"] == str(u)
        assert out["name"] == "test"

    def test_decimal_column_stringified(self):
        row = _FakeRow(amount=Decimal("99.99"))
        out = _row_to_dict(row)
        assert out["amount"] == "99.99"

    def test_deny_with_multiple_fields(self):
        row = _FakeRow(a=1, b=2, c=3, d=4)
        out = _row_to_dict(row, deny=frozenset({"b", "d"}))
        assert out == {"a": 1, "c": 3}

    def test_deny_empty_set_includes_all(self):
        row = _FakeRow(x=10)
        out = _row_to_dict(row, deny=frozenset())
        assert out == {"x": 10}

    def test_missing_attribute_returns_none(self):
        row = _FakeRow(a=1)
        row.__table__ = _FakeTable(["a", "ghost"])
        out = _row_to_dict(row)
        assert out["a"] == 1
        assert out["ghost"] is None

    def test_bool_column_preserved(self):
        row = _FakeRow(flag=True, count=0)
        out = _row_to_dict(row)
        assert out["flag"] is True
        assert out["count"] == 0

    def test_datetime_column_iso_format(self):
        dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)
        row = _FakeRow(created_at=dt)
        out = _row_to_dict(row)
        assert out["created_at"] == "2026-03-15T10:30:00+00:00"

    def test_list_column_jsonified(self):
        row = _FakeRow(tags=["a", "b"])
        out = _row_to_dict(row)
        assert out["tags"] == ["a", "b"]


# ---------------------------------------------------------------------------
# collect_user_data — comprehensive integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def full_user(db_session: AsyncSession):
    """A user with portfolios, backtests, API keys, webhooks, DSR, and legal acceptances."""
    uid = uuid.uuid4()
    user = User(
        id=uid,
        email="full@example.com",
        display_name="Full User",
        is_active=True,
        role="user",
        auth_provider="local",
    )
    db_session.add(user)
    await db_session.flush()

    p1 = Portfolio(
        id=uuid.uuid4(),
        user_id=uid,
        name="Portfolio A",
        description="First",
    )
    p2 = Portfolio(
        id=uuid.uuid4(),
        user_id=uid,
        name="Portfolio B",
        description="Second",
    )
    db_session.add_all([p1, p2])
    await db_session.flush()

    bt1 = BacktestResult(
        id=uuid.uuid4(),
        portfolio_id=p1.id,
        strategy_name="momentum",
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 3, 1, tzinfo=UTC),
        metrics={"sharpe": 1.5, "max_drawdown": -0.12},
        composite_score=0.87,
        score_breakdown={"risk": 0.9, "return": 0.84},
    )
    bt2 = BacktestResult(
        id=uuid.uuid4(),
        portfolio_id=p2.id,
        strategy_name="mean_reversion",
        start_date=datetime(2026, 2, 1, tzinfo=UTC),
        end_date=datetime(2026, 4, 1, tzinfo=UTC),
        metrics={"sharpe": 0.8},
    )
    db_session.add_all([bt1, bt2])
    await db_session.flush()

    api_key = ApiKey(
        id=uuid.uuid4(),
        user_id=uid,
        name="my-key",
        prefix="nexus-",
        key_hash="hashed_secret",
        scopes='["read","write"]',
    )
    webhook = WebhookConfig(
        id=uuid.uuid4(),
        user_id=uid,
        url="https://example.com/webhook",
        event_types='["trade","order"]',
        signing_secret="wh_secret_123",
    )
    db_session.add_all([api_key, webhook])
    await db_session.flush()

    return uid, p1, p2, bt1, bt2


class TestCollectUserDataExportStructure:
    async def test_export_has_all_expected_keys(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        expected_keys = {
            "schema_version",
            "exported_at",
            "user_id",
            "user",
            "portfolios",
            "backtests",
            "webhooks",
            "api_keys",
            "dsr_history",
            "legal_acceptances",
        }
        assert set(result.keys()) == expected_keys

    async def test_schema_version_is_one(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        assert result["schema_version"] == 1

    async def test_user_id_matches_input(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        assert result["user_id"] == str(uid)

    async def test_exported_at_is_recent(self, db_session, full_user):
        uid, *_ = full_user
        before = datetime.now(tz=UTC)
        result = await collect_user_data(db_session, uid)
        after = datetime.now(tz=UTC)
        exported = datetime.fromisoformat(result["exported_at"])
        assert before <= exported <= after

    async def test_all_collection_fields_are_lists(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        for key in ("portfolios", "backtests", "webhooks", "api_keys", "dsr_history", "legal_acceptances"):
            assert isinstance(result[key], list), f"{key} should be a list"


class TestCollectUserDataBacktestsJoin:
    async def test_multiple_portfolios_each_with_backtests(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="multi-portfolio@example.com",
            display_name="Multi Port",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        portfolios = []
        backtests = []
        for i in range(3):
            p = Portfolio(
                id=uuid.uuid4(),
                user_id=uid,
                name=f"P{i}",
                description="",
            )
            db_session.add(p)
            await db_session.flush()
            portfolios.append(p)

            bt = BacktestResult(
                id=uuid.uuid4(),
                portfolio_id=p.id,
                strategy_name=f"strat_{i}",
                start_date=datetime(2026, 1, 1, tzinfo=UTC),
                end_date=datetime(2026, 6, 1, tzinfo=UTC),
                metrics={"idx": i},
            )
            db_session.add(bt)
            await db_session.flush()
            backtests.append(bt)

        result = await collect_user_data(db_session, uid)
        assert len(result["backtests"]) == 3
        bt_ids = {b["id"] for b in result["backtests"]}
        for bt in backtests:
            assert str(bt.id) in bt_ids

    async def test_backtest_with_composite_score_included(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="scored@example.com",
            display_name="Scored",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        p = Portfolio(id=uuid.uuid4(), user_id=uid, name="Scored Port", description="")
        db_session.add(p)
        await db_session.flush()

        bt = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=p.id,
            strategy_name="scored_strat",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 2, 1, tzinfo=UTC),
            metrics={},
            composite_score=0.95,
            score_breakdown={"alpha": 0.9, "risk": 1.0},
        )
        db_session.add(bt)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        exported_bt = result["backtests"][0]
        assert exported_bt["composite_score"] == 0.95

    async def test_portfolio_without_backtests(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="empty-port@example.com",
            display_name="Empty Port",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        p = Portfolio(id=uuid.uuid4(), user_id=uid, name="No BT", description="")
        db_session.add(p)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        assert len(result["portfolios"]) == 1
        assert result["backtests"] == []

    async def test_backtest_belongs_to_other_user_portfolio_excluded(self, db_session):
        uid_a = uuid.uuid4()
        uid_b = uuid.uuid4()
        user_a = User(
            id=uid_a, email="a@x.com", display_name="A",
            is_active=True, role="user", auth_provider="local",
        )
        user_b = User(
            id=uid_b, email="b@x.com", display_name="B",
            is_active=True, role="user", auth_provider="local",
        )
        db_session.add_all([user_a, user_b])
        await db_session.flush()

        p_b = Portfolio(id=uuid.uuid4(), user_id=uid_b, name="B Port", description="")
        db_session.add(p_b)
        await db_session.flush()

        bt_b = BacktestResult(
            id=uuid.uuid4(),
            portfolio_id=p_b.id,
            strategy_name="secret",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 2, 1, tzinfo=UTC),
            metrics={},
        )
        db_session.add(bt_b)
        await db_session.flush()

        result_a = await collect_user_data(db_session, uid_a)
        bt_ids = [b["id"] for b in result_a["backtests"]]
        assert str(bt_b.id) not in bt_ids


class TestCollectUserDataDenyLists:
    async def test_user_pii_fields_excluded(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        for field in ("password_hash", "mfa_secret_encrypted", "mfa_backup_codes"):
            assert field not in result["user"], f"{field} should be excluded"

    async def test_api_key_hash_excluded(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        for key_data in result["api_keys"]:
            assert "key_hash" not in key_data

    async def test_webhook_signing_secret_excluded(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        for wh in result["webhooks"]:
            assert "signing_secret" not in wh

    async def test_user_visible_fields_included(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        assert result["user"]["email"] == "full@example.com"
        assert result["user"]["display_name"] == "Full User"
        assert result["user"]["is_active"] is True

    async def test_api_key_visible_fields_included(self, db_session, full_user):
        uid, *_ = full_user
        result = await collect_user_data(db_session, uid)
        key_data = result["api_keys"][0]
        assert key_data["name"] == "my-key"
        assert key_data["prefix"] == "nexus-"


class TestCollectUserDataDSRHistory:
    async def test_dsr_history_included_when_present(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="dsr-user@example.com",
            display_name="DSR User",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        dsr = await record_request(db_session, user_id=uid, kind="export", note="test export")
        await transition(db_session, dsr, status="completed")

        result = await collect_user_data(db_session, uid)
        assert len(result["dsr_history"]) == 1
        assert result["dsr_history"][0]["kind"] == "export"

    async def test_multiple_dsr_entries(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="multi-dsr@example.com",
            display_name="Multi DSR",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        for kind in ("export", "rectify", "restrict"):
            await record_request(db_session, user_id=uid, kind=kind)

        result = await collect_user_data(db_session, uid)
        assert len(result["dsr_history"]) == 3
        kinds = {d["kind"] for d in result["dsr_history"]}
        assert kinds == {"export", "rectify", "restrict"}


class TestCollectUserDataLegalAcceptances:
    async def test_legal_acceptances_empty_when_none(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="no-legal@example.com",
            display_name="No Legal",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        assert result["legal_acceptances"] == []

    async def test_legal_acceptances_included(self, db_session):
        uid = uuid.uuid4()
        user = User(
            id=uid,
            email="legal@example.com",
            display_name="Legal User",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        la = LegalAcceptance(
            id=uuid.uuid4(),
            user_id=uid,
            document_slug="terms-of-service",
            document_version="2.0",
            ip_address="127.0.0.1",
            user_agent="TestAgent/1.0",
        )
        db_session.add(la)
        await db_session.flush()

        result = await collect_user_data(db_session, uid)
        assert len(result["legal_acceptances"]) == 1
        assert result["legal_acceptances"][0]["document_slug"] == "terms-of-service"


class TestCollectUserDataErrorCases:
    async def test_nonexistent_user_raises_lookup_error(self, db_session):
        with pytest.raises(LookupError, match="user not found"):
            await collect_user_data(db_session, uuid.uuid4())

    async def test_lookup_error_includes_user_id(self, db_session):
        fake_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        with pytest.raises(LookupError, match=str(fake_id)):
            await collect_user_data(db_session, fake_id)


# ---------------------------------------------------------------------------
# DSR module — record_request, transition, list_user_requests
# ---------------------------------------------------------------------------


@pytest.fixture
async def dsr_user(db_session: AsyncSession):
    user = User(
        email=f"dsr_{uuid.uuid4().hex[:8]}@example.com",
        display_name="DSR Tester",
        is_active=True,
        role="user",
        auth_provider="local",
    )
    db_session.add(user)
    await db_session.flush()
    return user


class TestRecordRequest:
    @pytest.mark.parametrize("kind", sorted(DSR_KINDS))
    async def test_all_valid_kinds_accepted(self, db_session, dsr_user, kind):
        row = await record_request(db_session, user_id=dsr_user.id, kind=kind)
        assert row.kind == kind
        assert row.status == "pending"

    async def test_invalid_kind_raises(self, db_session, dsr_user):
        with pytest.raises(ValueError, match="unknown DSR kind"):
            await record_request(db_session, user_id=dsr_user.id, kind="invalid")

    async def test_negative_sla_days_raises(self, db_session, dsr_user):
        with pytest.raises(ValueError, match="sla_days must be positive"):
            await record_request(db_session, user_id=dsr_user.id, kind="export", sla_days=-1)

    async def test_zero_sla_days_raises(self, db_session, dsr_user):
        with pytest.raises(ValueError, match="sla_days must be positive"):
            await record_request(db_session, user_id=dsr_user.id, kind="export", sla_days=0)

    async def test_custom_sla_days(self, db_session, dsr_user):
        row = await record_request(
            db_session, user_id=dsr_user.id, kind="export", sla_days=15
        )
        assert row.sla_due_at is not None

    async def test_note_stored(self, db_session, dsr_user):
        row = await record_request(
            db_session, user_id=dsr_user.id, kind="delete", note="User requested via email"
        )
        assert row.note == "User requested via email"

    async def test_details_stored(self, db_session, dsr_user):
        row = await record_request(
            db_session, user_id=dsr_user.id, kind="export", details={"format": "json"}
        )
        assert row.details == {"format": "json"}

    async def test_default_details_empty_dict(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        assert row.details == {}


class TestTransition:
    async def test_pending_to_in_progress(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        updated = await transition(db_session, row, status="in_progress")
        assert updated.status == "in_progress"

    async def test_to_completed_sets_completed_at(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        updated = await transition(db_session, row, status="completed")
        assert updated.status == "completed"
        assert updated.completed_at is not None

    async def test_to_cancelled_sets_cancelled_at(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        updated = await transition(db_session, row, status="cancelled")
        assert updated.status == "cancelled"
        assert updated.cancelled_at is not None

    async def test_to_failed_no_timestamp(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        updated = await transition(db_session, row, status="failed")
        assert updated.status == "failed"
        assert updated.completed_at is None
        assert updated.cancelled_at is None

    async def test_idempotent_on_same_terminal_state(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        completed = await transition(db_session, row, status="completed")
        again = await transition(db_session, completed, status="completed")
        assert again.status == "completed"
        assert again.completed_at == completed.completed_at

    async def test_invalid_status_raises(self, db_session, dsr_user):
        row = await record_request(db_session, user_id=dsr_user.id, kind="export")
        with pytest.raises(ValueError, match="unknown DSR status"):
            await transition(db_session, row, status="bogus")


class TestListUserRequests:
    async def test_empty_for_new_user(self, db_session, dsr_user):
        rows = await list_user_requests(db_session, dsr_user.id)
        assert rows == []

    async def test_returns_in_reverse_chronological_order(self, db_session, dsr_user):
        r1 = await record_request(db_session, user_id=dsr_user.id, kind="export")
        r2 = await record_request(db_session, user_id=dsr_user.id, kind="delete")
        rows = await list_user_requests(db_session, dsr_user.id)
        assert len(rows) == 2
        assert rows[0].id == r2.id
        assert rows[1].id == r1.id

    async def test_scoped_to_user(self, db_session, dsr_user):
        other_user = User(
            email=f"other_{uuid.uuid4().hex[:8]}@example.com",
            display_name="Other",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(other_user)
        await db_session.flush()

        await record_request(db_session, user_id=dsr_user.id, kind="export")
        await record_request(db_session, user_id=other_user.id, kind="delete")

        rows = await list_user_requests(db_session, dsr_user.id)
        assert len(rows) == 1
        assert rows[0].kind == "export"
