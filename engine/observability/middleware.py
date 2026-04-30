"""ASGI middleware that binds a per-request correlation id + request id.

Reads ``X-Correlation-Id`` from the incoming request or generates a fresh
UUID4. The outbound response carries the same header. Each request gets a
distinct ``request_id`` so a single causal chain (one correlation id) can
span multiple HTTP requests while still being individually identifiable.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from engine.observability import context as ctx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


CORRELATION_HEADER = "X-Correlation-Id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind correlation/request ids to contextvars for the lifetime of a
    request, then echo the correlation header on the response."""

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_HEADER) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(self.header_name)
        cid = incoming if incoming else str(uuid.uuid4())
        rid = uuid.uuid4().hex
        sid = uuid.uuid4().hex[:16]

        ctx.bind_correlation_id(cid)
        ctx.bind_request_id(rid)
        ctx.new_span_id(sid)
        try:
            response = await call_next(request)
        finally:
            ctx.clear_context()
        response.headers[self.header_name] = cid
        return response


__all__ = ["CORRELATION_HEADER", "CorrelationIdMiddleware"]
