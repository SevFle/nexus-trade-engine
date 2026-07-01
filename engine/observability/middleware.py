"""ASGI middleware that binds a per-request correlation id + request id.

Reads ``X-Correlation-Id`` from the incoming HTTP request *or* the
WebSocket handshake headers, generating a fresh UUID4 when the client did
not supply a safe value. The outbound HTTP response carries the same
header; WebSocket frames have no per-message header channel, so for WS
connections the id is bound into the observability context for the full
connection lifecycle and the client correlates via its own inbound value
plus the server-side logs.

Each request/connection gets a distinct ``request_id`` so a single causal
chain (one correlation id) can span multiple HTTP requests while still
being individually identifiable.

This is a raw ASGI middleware (not a Starlette ``BaseHTTPMiddleware``) so
that it transparently handles both HTTP and WebSocket protocols and so
that streaming responses and ``BackgroundTasks`` continue to see the
bound correlation id while their generators / callbacks run.
``BaseHTTPMiddleware`` wraps the downstream app in a separate task and
resets its contextvars before background tasks execute; a raw middleware
does not, because its ``__call__`` only returns once the downstream app
(including background tasks) has finished.

The correlation id is bound to *both* observability channels:

* **structlog context** via :func:`structlog.contextvars.bind_contextvars`
  (merged into every record by ``merge_contextvars`` in
  :mod:`engine.observability.logging`); and
* **legacy observability context** via
  :func:`engine.observability.context.bind_request_scope`, which is read
  by the outbound HTTP client (:mod:`engine.observability.http_client`),
  the rate limiter (:mod:`engine.api.rate_limit`), the taskiq middleware
  (:mod:`engine.observability.taskiq_middleware`) and the
  ``add_correlation_context`` structlog processor.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

import structlog

from engine.observability import context as ctx

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


CORRELATION_HEADER = "X-Correlation-Id"
MAX_CORRELATION_ID_LENGTH = 128
# Visible ASCII only — blocks CR/LF (response splitting), control chars
# (terminal-control corruption), non-ASCII (header smuggling).
_VALID_CORRELATION_ID = re.compile(r"^[\x21-\x7e]{1,128}$")

# Scope types that carry HTTP-style headers we can read the incoming id
# from. ``lifespan`` and other scope types are passed through untouched.
_HANDLED_SCOPES = frozenset({"http", "websocket"})


def _safe_correlation_id(raw: str | None) -> str:
    """Return a safe correlation id: the raw value if it passes validation,
    otherwise a fresh UUID4. Never returns an attacker-controlled string."""
    if raw and _VALID_CORRELATION_ID.match(raw):
        return raw
    return str(uuid.uuid4())


def _read_header(scope: Scope, name_bytes: bytes) -> str | None:
    """Return the first value of a (lower-cased) header name, or ``None``."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == name_bytes:
            try:
                return raw_value.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


class CorrelationIdMiddleware:
    """Raw ASGI middleware handling both HTTP and WebSocket connections.

    Each request/connection runs in its own asyncio task and therefore its
    own contextvars copy — bindings go out of scope when the task ends, so
    there is no cross-request leakage. The explicit ``finally`` reset is
    kept so inlined-caller tests (which reuse a single task) stay clean.
    """

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        self._header_name_bytes = header_name.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in _HANDLED_SCOPES:
            await self.app(scope, receive, send)
            return

        cid = _safe_correlation_id(_read_header(scope, self._header_name_bytes))
        request_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]

        # structlog channel — merged into every record via merge_contextvars.
        structlog_tokens = structlog.contextvars.bind_contextvars(
            correlation_id=cid,
            request_id=request_id,
        )
        # Legacy observability channel — read by the http client, rate
        # limiter, taskiq middleware and the add_correlation_context
        # structlog processor.
        context_tokens = ctx.bind_request_scope(
            correlation_id=cid,
            request_id=request_id,
            span_id=span_id,
        )

        async def send_wrapper(message: Message) -> None:
            # Only HTTP responses carry a start line with headers we can
            # extend to echo the id back. WebSocket frames have no such
            # header channel; the client correlates via its inbound value.
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self._header_name_bytes, cid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # `await self.app(...)` only returns after the response body
            # AND any BackgroundTasks have completed, so resetting here
            # keeps the bindings live for the full request lifecycle —
            # including background task log lines.
            structlog.contextvars.reset_contextvars(**structlog_tokens)
            ctx.reset_tokens(context_tokens)


__all__ = ["CORRELATION_HEADER", "CorrelationIdMiddleware", "_safe_correlation_id"]
