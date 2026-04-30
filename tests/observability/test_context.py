"""Tests for the correlation/request/span context plumbing."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from engine.observability import context as ctx


@pytest.fixture(autouse=True)
def _clear_context():
    ctx.clear_context()
    yield
    ctx.clear_context()


class TestCorrelationIdLifecycle:
    def test_get_correlation_id_returns_none_when_unset(self):
        assert ctx.get_correlation_id() is None

    def test_bind_correlation_id_persists_within_scope(self):
        cid = "test-correlation-id"
        ctx.bind_correlation_id(cid)
        assert ctx.get_correlation_id() == cid

    def test_clear_context_removes_correlation_id(self):
        ctx.bind_correlation_id("x")
        ctx.clear_context()
        assert ctx.get_correlation_id() is None

    def test_ensure_correlation_id_generates_uuid_when_missing(self):
        cid = ctx.ensure_correlation_id()
        assert ctx.get_correlation_id() == cid
        parsed = uuid.UUID(cid)
        assert parsed.version == 4

    def test_ensure_correlation_id_preserves_existing(self):
        ctx.bind_correlation_id("preexisting")
        cid = ctx.ensure_correlation_id()
        assert cid == "preexisting"


class TestRequestAndSpanIds:
    def test_bind_request_id(self):
        ctx.bind_request_id("req-123")
        assert ctx.get_request_id() == "req-123"

    def test_new_span_id_generates_when_called_without_arg(self):
        sid = ctx.new_span_id()
        assert ctx.get_span_id() == sid
        assert len(sid) > 0

    def test_new_span_id_accepts_explicit_value(self):
        sid = ctx.new_span_id("explicit-span")
        assert sid == "explicit-span"
        assert ctx.get_span_id() == "explicit-span"

    def test_request_and_correlation_ids_are_independent(self):
        ctx.bind_correlation_id("corr-1")
        ctx.bind_request_id("req-1")
        assert ctx.get_correlation_id() == "corr-1"
        assert ctx.get_request_id() == "req-1"


class TestUserAndDomainContext:
    def test_bind_user_context_round_trip(self):
        ctx.bind_user_context(user_id="u-1", role="admin")
        snap = ctx.snapshot()
        assert snap["user_id"] == "u-1"
        assert snap["role"] == "admin"

    def test_bind_domain_context_round_trip(self):
        ctx.bind_domain_context(
            portfolio_id="p-1",
            strategy_id="s-1",
            broker="alpaca",
            order_id="o-1",
        )
        snap = ctx.snapshot()
        assert snap["portfolio_id"] == "p-1"
        assert snap["strategy_id"] == "s-1"
        assert snap["broker"] == "alpaca"
        assert snap["order_id"] == "o-1"

    def test_snapshot_excludes_unset_values(self):
        ctx.bind_correlation_id("c-1")
        snap = ctx.snapshot()
        assert "correlation_id" in snap
        assert "request_id" not in snap


class TestAsyncIsolation:
    @pytest.mark.asyncio
    async def test_correlation_id_isolated_between_tasks(self):
        seen: dict[str, str | None] = {}

        async def worker(name: str, cid: str):
            ctx.bind_correlation_id(cid)
            await asyncio.sleep(0.01)
            seen[name] = ctx.get_correlation_id()

        await asyncio.gather(
            worker("a", "id-a"),
            worker("b", "id-b"),
        )

        assert seen["a"] == "id-a"
        assert seen["b"] == "id-b"

    @pytest.mark.asyncio
    async def test_use_correlation_context_manager(self):
        ctx.bind_correlation_id("outer")
        async with ctx.use_correlation_id("inner"):
            assert ctx.get_correlation_id() == "inner"
        assert ctx.get_correlation_id() == "outer"
