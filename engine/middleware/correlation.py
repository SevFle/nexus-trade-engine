"""FastAPI/Starlette correlation-id middleware.

This module hosts the :class:`CorrelationIdMiddleware` built on Starlette's
:class:`~starlette.middleware.base.BaseHTTPMiddleware`. It is kept for
deployments that prefer the ``Request``/``call_next`` dispatch style and
the ergonomic response-header mutation it enables.

A second, lower-level implementation — the **raw-ASGI** middleware in
:mod:`engine.observability.middleware` — is the one the app factory
(:func:`engine.app.create_app`) registers by default. The two variants are
both exported from this package so callers can discover them in one place:

* :class:`engine.middleware.correlation.CorrelationIdMiddleware` —
  ``BaseHTTPMiddleware`` based; HTTP only.
* :class:`engine.observability.middleware.CorrelationIdMiddleware` —
  raw ASGI; handles both HTTP and WebSocket and preserves the correlation
  binding for the full request lifecycle (streaming responses and
  ``BackgroundTasks``). This is the recommended default.

A thin ``BaseHTTPMiddleware`` that gives every HTTP request a single
``X-Correlation-Id`` and threads it through every observable channel:

1. **Read or generate.** The id is taken from the incoming
   ``X-Correlation-Id`` header when the client supplied a *safe* value,
   otherwise a fresh UUID4 is minted. Untrusted values (CRLF, control
   chars, non-ASCII, oversized) are discarded and regenerated to prevent
   response-splitting / header-smuggling attacks. The hardening lives in
   :func:`engine.observability.middleware._safe_correlation_id`, shared
   with the raw-ASGI middleware so both transports apply identical rules.

2. **structlog context.** The id is bound to the *structlog* context via
   :func:`structlog.contextvars.bind_contextvars` so every log record
   produced while handling the request carries ``correlation_id``
   (structlog is wired up with ``merge_contextvars`` in
   :mod:`engine.observability.logging`). A per-request ``request_id`` is
   bound alongside so a single causal chain can still be split into its
   individual HTTP requests.

3. **Legacy observability context.** The same triple (correlation id,
   request id, span id) is mirrored onto :mod:`engine.observability.context`
   so existing integrations keep working without changes:

   * :mod:`engine.observability.http_client` injects ``X-Correlation-Id``
     on outbound calls.
   * :mod:`engine.api.rate_limit` tags rate-limit rejections.
   * :mod:`engine.observability.taskiq_middleware` propagates the id into
     background tasks.
   * the ``add_correlation_context`` structlog processor enriches records.

4. **Response header.** The id is echoed back on the outbound
   ``X-Correlation-Id`` header so callers can correlate client-side and
   server-side logs.

Both context bindings are reset in a ``finally`` block so nothing leaks
between requests that happen to share a task context (notably in tests).

Note: the raw-ASGI variant additionally covers WebSocket connections and
``BackgroundTasks`` (whose log lines would otherwise be emitted after this
middleware has reset its bindings), which is why it is the default in
:func:`engine.app.create_app`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from engine.observability import context as ctx
from engine.observability.middleware import (
    CORRELATION_HEADER,
    _safe_correlation_id,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

__all__ = ["CORRELATION_HEADER", "CorrelationIdMiddleware"]


def _new_span_id() -> str:
    """Short, unique-per-request span id (matches the raw-ASGI middleware)."""
    return uuid.uuid4().hex[:16]


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind an ``X-Correlation-Id`` to structlog + observability context.

    HTTP-only. For WebSocket connections and ``BackgroundTasks`` use the
    raw-ASGI :class:`engine.observability.middleware.CorrelationIdMiddleware`
    instead — it is the default registered by the app factory.

    Parameters
    ----------
    app:
        The wrapped ASGI application.
    header_name:
        Header used to read the incoming id and write the outgoing one.
        Defaults to ``X-Correlation-Id``. HTTP headers are case-insensitive
        so this interoperates with callers that send ``X-Correlation-ID``.
    """

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_HEADER) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        correlation_id = _safe_correlation_id(request.headers.get(self.header_name))
        request_id = uuid.uuid4().hex
        span_id = _new_span_id()

        structlog_tokens = structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            request_id=request_id,
        )

        context_tokens = ctx.bind_request_scope(
            correlation_id=correlation_id,
            request_id=request_id,
            span_id=span_id,
        )

        try:
            response = await call_next(request)
        finally:
            # NOTE: reset happens before BackgroundTasks execute because
            # BaseHTTPMiddleware runs them in a separate context. For
            # correlation on background task log lines, use the raw-ASGI
            # middleware (the app default) instead.
            structlog.contextvars.reset_contextvars(**structlog_tokens)
            ctx.reset_tokens(context_tokens)

        response.headers[self.header_name] = correlation_id
        return response
