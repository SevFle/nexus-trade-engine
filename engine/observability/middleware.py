"""ASGI middleware that binds a per-request correlation id + request id.

Reads ``X-Correlation-Id`` from the incoming request or generates a fresh
UUID4. The outbound response carries the same header. Each request gets a
distinct ``request_id`` so a single causal chain (one correlation id) can
span multiple HTTP requests while still being individually identifiable.

This is a raw ASGI middleware (not a Starlette ``BaseHTTPMiddleware``) so
that streaming responses and ``BackgroundTasks`` continue to see the
bound correlation id while their generators / callbacks run.

.. warning::

    **Do not port this to ``BaseHTTPMiddleware``.** That base class has a
    well-known *streaming timing hazard* that silently drops the
    correlation context exactly when you most need it (on slow / streaming
    bodies and on background work).

    ``BaseHTTPMiddleware.dispatch`` wraps the downstream app and exposes
    its output via ``await call_next(...)``. The returned ``Response``
    carries a lazy ``body_iterator``; the bytes are **not** consumed inside
    ``dispatch``. The standard pattern::

        async def dispatch(self, request, call_next):
            token = ctx_var.set(value)
            try:
                response = await call_next(request)   # returns immediately
                response.headers[HEADER] = value
                return response
            finally:
                token.reset()                          # runs NOW

    looks correct but the ``finally`` — and therefore ``token.reset()`` —
    runs as soon as ``response`` is *returned*, **before** Starlette starts
    iterating ``response.body_iterator`` to flush chunks to the client.
    Consequently:

      * Streaming endpoints (``StreamingResponse``, SSE, large downloads,
        chunked uploads) see ``ctx_var.get() == None`` while generating
        every body chunk. Log records and span attributes emitted from
        inside the generator lose their correlation id.
      * ``BackgroundTasks`` attached to the response run after ``dispatch``
        returns, so they too observe a reset (empty) context.

    The raw ASGI implementation below avoids this entirely: ``await
    self.app(scope, receive, send)`` does not return until *every* body
    chunk (including streamed ones) has been pushed through ``send``, so
    the contextvars reset in the ``finally`` happens only after the full
    response — streaming body included — has been emitted. We also reset
    the tokens in a ``finally`` so the inlined-caller (unit-test) case is
    covered; in production each request runs in its own asyncio Task and
    the context is collected when the task ends regardless.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from starlette.datastructures import State

from engine.observability import context as ctx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send


CORRELATION_HEADER = "X-Correlation-Id"
MAX_CORRELATION_ID_LENGTH = 128
# Canonical lowercased-bytes form of the default header name. ASGI
# guarantees header names arrive lowercased on ``scope['headers']``, so the
# middleware compares against exactly these bytes. Exposed at module level
# as the single source of truth so tests can import the real attribute
# instead of re-deriving it (which risks drifting from the middleware's
# own casing).
_HEADER_NAME_BYTES = CORRELATION_HEADER.lower().encode("latin-1")
# Visible ASCII only — blocks CR/LF (response splitting), control chars
# (terminal-control corruption), non-ASCII (header smuggling).
_VALID_CORRELATION_ID = re.compile(r"^[\x21-\x7e]{1,128}$")


def safe_correlation_id(raw: str | None) -> str:
    """Return a safe correlation id: the raw value if it passes validation,
    otherwise a fresh UUID4. Never returns an attacker-controlled string.

    Public so the HTTP middleware and the taskiq broker middleware share
    one vetted validator (label values arrive from Redis and may have been
    crafted by a malicious producer).
    """
    if raw and _VALID_CORRELATION_ID.match(raw):
        return raw
    return str(uuid.uuid4())


# Backwards-compatible alias. Prefer the public ``safe_correlation_id``;
# this keeps older import sites working during the rename.
_safe_correlation_id = safe_correlation_id


class CorrelationIdMiddleware:
    """Raw ASGI middleware. Each request runs in its own asyncio task and
    therefore its own contextvars copy — there is no need to clear the
    bound values; they go out of scope when the task finishes."""

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        # Reuse the module-level canonical bytes for the default header so the
        # middleware and any external consumer (tests, other middleware) share
        # one vetted byte string. A custom ``header_name`` still gets its own
        # lowercased form.
        self._header_name_bytes = (
            _HEADER_NAME_BYTES
            if header_name == CORRELATION_HEADER
            else header_name.lower().encode("latin-1")
        )

    def _extract_incoming_cid(self, scope: Scope) -> str | None:
        """Return the configured correlation-id header from the ASGI scope, if present.

        The raw value is decoded best-effort; an undecodable header yields
        ``None`` so :func:`safe_correlation_id` mints a fresh UUID4 instead.
        """
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == self._header_name_bytes:
                try:
                    return raw_value.decode("latin-1")
                except UnicodeDecodeError:
                    return None
        return None

    def _ensure_request_state(self, scope: Scope) -> State:
        """Return a Starlette ``State`` on the scope for uniform attribute access.

        We install a ``State`` rather than a bare dict so the contract matches
        Starlette's own request/app state. If an outer component already
        installed a ``State`` we reuse it; if it installed a bare dict
        (Starlette's own lazy ``Request.state`` does this on first access) we
        migrate its contents into a fresh ``State`` so nothing is silently
        dropped.
        """
        existing_state = scope.get("state")
        if isinstance(existing_state, State):
            return existing_state
        request_state = State(
            dict(existing_state) if isinstance(existing_state, dict) else {}
        )
        scope["state"] = request_state
        return request_state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cid = safe_correlation_id(self._extract_incoming_cid(scope))
        request_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]

        request_state = self._ensure_request_state(scope)
        request_state.correlation_id = cid
        request_state.request_id = request_id
        request_state.span_id = span_id

        # Initialize both token handles up front so the ``finally`` is
        # safe even if one of the bind calls raises partway through — a
        # half-bound context must still be torn down cleanly.
        tokens: list[Any] | None = None
        structlog_tokens: Mapping[str, Any] | None = None

        # Tracks whether *any* ``http.response.start`` has already been
        # pushed downstream. Used both to avoid duplicate headers on the
        # error path and to decide whether we must synthesize our own
        # error response when an exception escapes the inner app.
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers", []))
                headers.append((self._header_name_bytes, cid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            tokens = ctx.bind_request_scope(
                correlation_id=cid,
                request_id=request_id,
                span_id=span_id,
            )
            # Mirror the binding into structlog's own contextvars so the
            # ``structlog.contextvars.merge_contextvars`` processor (see
            # ``engine.observability.logging``) picks up the same ids
            # without having to route through ``ctx.snapshot``.
            structlog_tokens = structlog.contextvars.bind_contextvars(
                correlation_id=cid,
                request_id=request_id,
                span_id=span_id,
            )

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                # An exception escaped the inner app. If it is an
                # ``HTTPException`` / validation error it has *already* been
                # turned into a 4xx/5xx response by Starlette's
                # ``ExceptionMiddleware`` (which sits inside us), so that
                # response's ``http.response.start`` flowed through
                # ``send_wrapper`` and already carries the header and set
                # ``response_started`` — nothing more to do.
                #
                # An *unhandled* exception, however, propagates past
                # ``ExceptionMiddleware`` and would be caught by Starlette's
                # outer ``ServerErrorMiddleware`` (which sits *outside* us) to
                # produce a 500. That 500 is generated outside this middleware,
                # so it would bypass ``send_wrapper`` and ship **without** the
                # correlation header. To guarantee the header is present on
                # every response — success, client-error, and server-error —
                # we synthesize a minimal 500 here (with the header) when no
                # response has started yet, then re-raise so
                # ``ServerErrorMiddleware`` can still log the traceback.
                # ``ServerErrorMiddleware`` records ``response_started`` from
                # our synthesized response and therefore suppresses its own
                # (duplicate) 500, avoiding a double-response.
                if not response_started:
                    # Imported lazily so importing this module never pulls in
                    # the full Starlette response stack (keeps the validator
                    # usable from contexts that only need ``safe_correlation_id``).
                    from starlette.responses import JSONResponse  # noqa: PLC0415

                    error_response = JSONResponse(
                        status_code=500,
                        content={"detail": "Internal Server Error"},
                    )
                    await error_response(scope, receive, send_wrapper)
                raise
        finally:
            # By the time `await self.app(...)` returns (or the exception
            # propagates) all body chunks have been sent — including
            # streaming responses. Reset is therefore safe and prevents
            # leakage in inlined-caller tests.
            #
            # Each binding is torn down independently and guarded:
            # ``reset_contextvars`` raises ``ValueError`` if a token was
            # already invalidated (e.g. by a nested context that reset it
            # first, or by test teardown); swallowing that keeps the
            # custom-context reset below from being skipped.
            if structlog_tokens is not None:
                with contextlib.suppress(LookupError, ValueError):
                    structlog.contextvars.reset_contextvars(**structlog_tokens)
            if tokens is not None:
                ctx.reset_tokens(tokens)


__all__ = [
    "CORRELATION_HEADER",
    "_HEADER_NAME_BYTES",
    "CorrelationIdMiddleware",
    "_safe_correlation_id",
    "safe_correlation_id",
]
