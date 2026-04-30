"""taskiq broker middleware that propagates correlation context across
the producer/consumer boundary using message labels."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from taskiq import TaskiqMiddleware

from engine.observability import context as ctx
from engine.observability.middleware import _safe_correlation_id

if TYPE_CHECKING:
    from taskiq import TaskiqMessage

_MAX_REQUEST_ID_LEN = 64


class CorrelationMiddleware(TaskiqMiddleware):
    """Send: copy bound correlation id into message labels.
    Receive: bind labels back into context for the worker's task scope.

    No `post_execute` clear — taskiq runs each task in its own asyncio
    Task, so contextvars are isolated and clean up when the task ends.
    Calling `clear_context()` in `post_execute` would clobber a sibling
    task's context if the broker shares an event loop without
    `asyncio.create_task` boundaries.
    """

    async def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        cid = ctx.get_correlation_id()
        if cid:
            message.labels["correlation_id"] = cid
        rid = ctx.get_request_id()
        if rid:
            message.labels["request_id"] = rid
        return message

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        # Labels arrive from Redis and may have been crafted by a malicious
        # producer; validate before binding into our log records.
        raw_cid = message.labels.get("correlation_id")
        ctx.bind_correlation_id(_safe_correlation_id(raw_cid))
        raw_rid = message.labels.get("request_id")
        if raw_rid and len(raw_rid) <= _MAX_REQUEST_ID_LEN:
            ctx.bind_request_id(raw_rid)
        ctx.new_span_id(uuid.uuid4().hex[:16])
        return message


__all__ = ["CorrelationMiddleware"]
