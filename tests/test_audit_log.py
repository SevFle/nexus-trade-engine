"""Tests for engine.core.audit_log — hash-chained immutable audit trail."""

from __future__ import annotations

from dataclasses import replace

import pytest

from engine.core.audit_log import (
    AuditEvent,
    AuditLogError,
    AuditService,
    InMemoryAuditLog,
)


@pytest.fixture
def service():
    return AuditService(log=InMemoryAuditLog())


class TestAppend:
    @pytest.mark.asyncio
    async def test_first_event_links_genesis(self, service):
        ev = await service.append(
            event_type="login",
            actor_id="u-1",
            payload={"ip": "1.2.3.4"},
        )
        assert ev.sequence == 1
        assert ev.prev_hash == "0" * 64
        assert len(ev.hash) == 64

    @pytest.mark.asyncio
    async def test_sequential_chain(self, service):
        a = await service.append("login", "u-1", {})
        b = await service.append("trade", "u-1", {"symbol": "AAPL"})
        assert b.sequence == a.sequence + 1
        assert b.prev_hash == a.hash

    @pytest.mark.asyncio
    async def test_hash_depends_on_payload(self, service):
        a = await service.append("login", "u-1", {"ip": "1.1.1.1"})
        b = await service.append("login", "u-1", {"ip": "2.2.2.2"})
        assert a.hash != b.hash


class TestVerifyChain:
    @pytest.mark.asyncio
    async def test_unmodified_chain_verifies(self, service):
        await service.append("login", "u-1", {})
        await service.append("trade", "u-1", {})
        await service.append("logout", "u-1", {})
        assert await service.verify_chain() is True

    @pytest.mark.asyncio
    async def test_empty_chain_verifies(self, service):
        assert await service.verify_chain() is True

    @pytest.mark.asyncio
    async def test_tampered_payload_detected(self, service):
        await service.append("login", "u-1", {"ip": "1.1.1.1"})
        b = await service.append("trade", "u-1", {"symbol": "AAPL"})
        log = service._log
        tampered = replace(b, payload={"symbol": "MSFT"})
        log._events[b.sequence - 1] = tampered
        assert await service.verify_chain() is False

    @pytest.mark.asyncio
    async def test_broken_link_detected(self, service):
        await service.append("login", "u-1", {})
        b = await service.append("trade", "u-1", {})
        log = service._log
        bad = replace(b, prev_hash="0" * 64)
        log._events[b.sequence - 1] = bad
        assert await service.verify_chain() is False


class TestQueries:
    @pytest.mark.asyncio
    async def test_list_returns_in_order(self, service):
        a = await service.append("login", "u-1", {})
        b = await service.append("trade", "u-1", {})
        c = await service.append("logout", "u-1", {})
        out = await service.list_events()
        assert [e.sequence for e in out] == [a.sequence, b.sequence, c.sequence]

    @pytest.mark.asyncio
    async def test_filter_by_actor(self, service):
        await service.append("login", "u-1", {})
        await service.append("login", "u-2", {})
        await service.append("trade", "u-1", {})
        out = await service.list_events(actor_id="u-1")
        assert {e.actor_id for e in out} == {"u-1"}
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_get_by_sequence(self, service):
        a = await service.append("login", "u-1", {})
        out = await service.get_by_sequence(a.sequence)
        assert out is not None and out.id == a.id

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, service):
        assert await service.get_by_sequence(999) is None


class TestRedaction:
    @pytest.mark.asyncio
    async def test_password_payload_redacted_at_append(self, service):
        ev = await service.append(
            "login",
            "u-1",
            {"username": "alice", "password": "hunter2"},
        )
        assert ev.payload["password"] != "hunter2"


class TestValidation:
    @pytest.mark.asyncio
    async def test_empty_event_type_rejected(self, service):
        with pytest.raises(AuditLogError):
            await service.append("", "u-1", {})

    @pytest.mark.asyncio
    async def test_empty_actor_rejected(self, service):
        with pytest.raises(AuditLogError):
            await service.append("login", "", {})


class TestEntity:
    def test_audit_event_dataclass(self):
        e = AuditEvent(
            id="e1",
            sequence=1,
            event_type="login",
            actor_id="u-1",
            payload={},
            prev_hash="0" * 64,
            hash="a" * 64,
            created_at_epoch=1.0,
        )
        assert e.sequence == 1
