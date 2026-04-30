"""Tests for taskiq broker middleware that propagates correlation ids."""

from __future__ import annotations

import pytest
from taskiq import TaskiqMessage

from engine.observability import context as ctx
from engine.observability.taskiq_middleware import CorrelationMiddleware


@pytest.fixture(autouse=True)
def _clear_context():
    ctx.clear_context()
    yield
    ctx.clear_context()


def _make_message(labels: dict | None = None) -> TaskiqMessage:
    return TaskiqMessage(
        task_id="t-1",
        task_name="some.task",
        labels=labels or {},
        labels_types=None,
        args=[],
        kwargs={},
    )


class TestSendSide:
    @pytest.mark.asyncio
    async def test_pre_send_attaches_correlation_id_label(self):
        ctx.bind_correlation_id("c-1")
        m = CorrelationMiddleware()
        msg = _make_message()
        out = await m.pre_send(msg)
        assert out.labels.get("correlation_id") == "c-1"

    @pytest.mark.asyncio
    async def test_pre_send_noop_when_unbound(self):
        m = CorrelationMiddleware()
        msg = _make_message()
        out = await m.pre_send(msg)
        assert "correlation_id" not in out.labels


class TestReceiveSide:
    @pytest.mark.asyncio
    async def test_pre_execute_binds_label_to_context(self):
        m = CorrelationMiddleware()
        msg = _make_message(labels={"correlation_id": "c-from-label"})
        await m.pre_execute(msg)
        assert ctx.get_correlation_id() == "c-from-label"

    @pytest.mark.asyncio
    async def test_pre_execute_generates_id_when_missing(self):
        m = CorrelationMiddleware()
        msg = _make_message()
        await m.pre_execute(msg)
        cid = ctx.get_correlation_id()
        assert cid is not None
        assert len(cid) >= 16

    @pytest.mark.asyncio
    async def test_pre_execute_rejects_invalid_label(self):
        # Header-injection-style label must not be bound verbatim; a
        # fresh UUID is generated instead.
        m = CorrelationMiddleware()
        msg = _make_message(labels={"correlation_id": "evil\r\nLog-Inject: 1"})
        await m.pre_execute(msg)
        cid = ctx.get_correlation_id()
        assert cid is not None
        assert "\r" not in cid
        assert "\n" not in cid

    @pytest.mark.asyncio
    async def test_pre_execute_rejects_oversize_label(self):
        m = CorrelationMiddleware()
        msg = _make_message(labels={"correlation_id": "x" * 10_000})
        await m.pre_execute(msg)
        cid = ctx.get_correlation_id()
        assert cid is not None
        assert len(cid) <= 128
