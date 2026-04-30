"""ASGI middleware that binds a per-request correlation id + request id.

Reads ``X-Correlation-Id`` from the incoming request or generates a fresh
UUID4. The outbound response carries the same header. Each request gets a
distinct ``request_id`` so a single causal chain (one correlation id) can
span multiple HTTP requests while still being individually identifiable.

This is a raw ASGI middleware (not a Starlette ``BaseHTTPMiddleware``) so
that streaming responses and ``BackgroundTasks`` continue to see the
bound correlation id while their generators / callbacks run.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from engine.observability import context as ctx

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


CORRELATION_HEADER = "X-Correlation-Id"
MAX_CORRELATION_ID_LENGTH = 128
# Visible ASCII only — blocks CR/LF (response splitting), control chars
# (terminal-control corruption), non-ASCII (header smuggling).
_VALID_CORRELATION_ID = re.compile(r"^[\x21-\x7e]{1,128}$")


def _safe_correlation_id(raw: str | None) -> str:
    """Return a safe correlation id: the raw value if it passes validation,
    otherwise a fresh UUID4. Never returns an attacker-controlled string."""
    if raw and _VALID_CORRELATION_ID.match(raw):
        return raw
    return str(uuid.uuid4())


class CorrelationIdMiddleware:
    """Raw ASGI middleware. Each request runs in its own asyncio task and
    therefore its own contextvars copy — there is no need to clear the
    bound values; they go out of scope when the task finishes."""

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        self._header_name_bytes = header_name.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming: str | None = None
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == self._header_name_bytes:
                try:
                    incoming = raw_value.decode("latin-1")
                except UnicodeDecodeError:
                    incoming = None
                break

        cid = _safe_correlation_id(incoming)
        tokens = ctx.bind_request_scope(
            correlation_id=cid,
            request_id=uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:16],
        )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(
                    (self._header_name_bytes, cid.encode("latin-1"))
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # By the time `await self.app(...)` returns, all body chunks
            # have been sent — including streaming responses. Reset is
            # therefore safe and prevents leakage in inlined-caller tests.
            ctx.reset_tokens(tokens)


__all__ = ["CORRELATION_HEADER", "CorrelationIdMiddleware", "_safe_correlation_id"]
