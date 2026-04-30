"""Tests for engine.api.sessions — session lifecycle + concurrent limits."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from engine.api.sessions import (
    InMemorySessionStore,
    Session,
    SessionConfig,
    SessionExpiredError,
    SessionRevokedError,
    SessionService,
    hash_ip,
    hash_user_agent,
)


def _user() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def store():
    return InMemorySessionStore()


@pytest.fixture
def service(store):
    return SessionService(
        store=store,
        config=SessionConfig(
            idle_timeout_sec=900,
            absolute_timeout_sec=86_400,
            max_concurrent=3,
        ),
    )


class TestCreateAndRetrieve:
    @pytest.mark.asyncio
    async def test_create_returns_session_with_id(self, service: SessionService):
        s = await service.create(
            user_id=_user(),
            device_label="iPhone",
            ip="1.2.3.4",
            user_agent="Mozilla/5.0",
        )
        assert s.id
        assert isinstance(s.id, str)
        assert s.revoked is False

    @pytest.mark.asyncio
    async def test_get_returns_existing_session(self, service: SessionService):
        s = await service.create(
            user_id=_user(),
            device_label="d",
            ip="1.2.3.4",
            user_agent="ua",
        )
        out = await service.get(s.id)
        assert out is not None
        assert out.id == s.id

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown(self, service: SessionService):
        assert await service.get("nope") is None


class TestIdleTimeout:
    @pytest.mark.asyncio
    async def test_idle_timeout_expires_session(self, store):
        svc = SessionService(
            store=store,
            config=SessionConfig(
                idle_timeout_sec=10, absolute_timeout_sec=86_400
            ),
        )
        s = await svc.create(_user(), "d", "1.2.3.4", "ua")
        await store.save(
            Session(
                id=s.id,
                user_id=s.user_id,
                device_label=s.device_label,
                ip_hash=s.ip_hash,
                ua_hash=s.ua_hash,
                created_at=s.created_at,
                last_active_at=s.last_active_at - timedelta(seconds=11),
                revoked=False,
            )
        )
        with pytest.raises(SessionExpiredError):
            await svc.touch(s.id)

    @pytest.mark.asyncio
    async def test_touch_extends_last_active(self, service: SessionService):
        s = await service.create(_user(), "d", "1.2.3.4", "ua")
        before = s.last_active_at
        out = await service.touch(s.id)
        assert out.last_active_at >= before


class TestAbsoluteTimeout:
    @pytest.mark.asyncio
    async def test_absolute_timeout_kills_long_lived_session(self, store):
        svc = SessionService(
            store=store,
            config=SessionConfig(
                idle_timeout_sec=86_400, absolute_timeout_sec=10
            ),
        )
        s = await svc.create(_user(), "d", "1.2.3.4", "ua")
        await store.save(
            Session(
                id=s.id,
                user_id=s.user_id,
                device_label=s.device_label,
                ip_hash=s.ip_hash,
                ua_hash=s.ua_hash,
                created_at=s.created_at - timedelta(seconds=11),
                last_active_at=s.last_active_at,
                revoked=False,
            )
        )
        with pytest.raises(SessionExpiredError):
            await svc.touch(s.id)


class TestConcurrentLimit:
    @pytest.mark.asyncio
    async def test_creating_above_limit_revokes_oldest(self, store):
        svc = SessionService(
            store=store,
            config=SessionConfig(
                idle_timeout_sec=900,
                absolute_timeout_sec=86_400,
                max_concurrent=2,
            ),
        )
        u = _user()
        a = await svc.create(u, "phone", "1.2.3.4", "ua")
        b = await svc.create(u, "laptop", "1.2.3.4", "ua")
        c = await svc.create(u, "tablet", "1.2.3.4", "ua")
        out_a = await store.get(a.id)
        out_b = await store.get(b.id)
        out_c = await store.get(c.id)
        assert out_a is not None and out_a.revoked is True
        assert out_b is not None and out_b.revoked is False
        assert out_c is not None and out_c.revoked is False


class TestRevocation:
    @pytest.mark.asyncio
    async def test_explicit_revoke_blocks_touch(self, service: SessionService):
        s = await service.create(_user(), "d", "1.2.3.4", "ua")
        await service.revoke(s.id)
        with pytest.raises(SessionRevokedError):
            await service.touch(s.id)

    @pytest.mark.asyncio
    async def test_revoke_all_for_user_kills_all_sessions(
        self, service: SessionService, store
    ):
        u = _user()
        a = await service.create(u, "a", "1.2.3.4", "ua")
        b = await service.create(u, "b", "1.2.3.4", "ua")
        n = await service.revoke_all_for_user(u)
        assert n == 2
        for sid in (a.id, b.id):
            out = await store.get(sid)
            assert out is not None and out.revoked is True


class TestListing:
    @pytest.mark.asyncio
    async def test_list_for_user_excludes_revoked_by_default(
        self, service: SessionService
    ):
        u = _user()
        a = await service.create(u, "a", "1.2.3.4", "ua")
        await service.create(u, "b", "1.2.3.4", "ua")
        await service.revoke(a.id)
        active = await service.list_active_for_user(u)
        labels = [s.device_label for s in active]
        assert "b" in labels
        assert "a" not in labels


class TestPrivacyHashing:
    def test_ip_is_hashed_not_stored_plain(self):
        h = hash_ip("203.0.113.42", salt="s1")
        assert "203.0.113.42" not in h
        assert len(h) >= 32

    def test_user_agent_is_hashed(self):
        h = hash_user_agent("Mozilla/5.0", salt="s1")
        assert "Mozilla" not in h

    def test_hashing_same_input_with_same_salt_is_stable(self):
        a = hash_ip("203.0.113.42", salt="s1")
        b = hash_ip("203.0.113.42", salt="s1")
        assert a == b

    def test_hashing_different_salts_differ(self):
        a = hash_ip("203.0.113.42", salt="s1")
        b = hash_ip("203.0.113.42", salt="s2")
        assert a != b


class TestSessionEntity:
    def test_session_dataclass_fields(self):
        s = Session(
            id="x",
            user_id=uuid.uuid4(),
            device_label="d",
            ip_hash="h1",
            ua_hash="h2",
            created_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
            revoked=False,
        )
        assert s.id == "x"
        assert s.revoked is False
