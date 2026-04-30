"""taskiq broker middleware that propagates correlation context across
the producer/consumer boundary using message labels."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from taskiq import TaskiqMiddleware

from engine.observability import context as ctx

if TYPE_CHECKING:
    from taskiq import TaskiqMessage, TaskiqResult


class CorrelationMiddleware(TaskiqMiddleware):
    """Send: copy bound correlation id into message labels.
    Receive: bind labels back into context for the worker's task scope."""

    async def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        cid = ctx.get_correlation_id()
        if cid:
            message.labels["correlation_id"] = cid
        rid = ctx.get_request_id()
        if rid:
            message.labels["request_id"] = rid
        return message

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        cid = message.labels.get("correlation_id") or str(uuid.uuid4())
        ctx.bind_correlation_id(cid)
        rid = message.labels.get("request_id")
        if rid:
            ctx.bind_request_id(rid)
        ctx.new_span_id()
        return message

    async def post_execute(
        self,
        message: TaskiqMessage,  # noqa: ARG002 - taskiq protocol
        result: TaskiqResult,  # noqa: ARG002 - taskiq protocol
    ) -> None:
        ctx.clear_context()


__all__ = ["CorrelationMiddleware"]
