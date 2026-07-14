"""Tests for the FastAPI correlation middleware."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import pytest
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from engine.observability import context as ctx
from engine.observability.middleware import (
    CorrelationIdMiddleware,
    safe_correlation_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def _reset_logging_context() -> None:
    """Reset all per-request observability state.

    Clears both the structlog contextvars (bound by the
    ``BaseHTTPMiddleware`` variant) and the engine ``contextvars``
    context (bound by the raw-ASGI variant and read directly by the
    test app routes). Calling this before *and* after every test keeps
    one test's bindings from leaking into a later test that happens to
    run in the same asyncio task.
    """
    structlog.contextvars.clear_contextvars()
    ctx.clear_context()


@pytest.fixture(autouse=True)
def _isolate_logging_context() -> Iterator[None]:
    """Guarantee a clean logging/correlation context around every test.

    Several tests in this module bind ids into shared contextvars (either
    directly via ``ctx`` or indirectly through the middleware). Without an
    explicit reset, a binding made in one test could be observed by a
    later test running in the same task, producing flaky, order-dependent
    failures. We reset before yielding *and* again in teardown so the
    next test always starts (and we always leave) a blank slate even if
    a test raises or forgets to clean up after itself.
    """
    _reset_logging_context()
    try:
        yield
    finally:
        _reset_logging_context()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    async def echo() -> dict:
        return {
            "correlation_id": ctx.get_correlation_id(),
            "request_id": ctx.get_request_id(),
        }

    return app


def _build_error_app() -> FastAPI:
    """App with routes that exercise every error path."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/http-error")
    async def http_error() -> dict:
        raise HTTPException(status_code=404, detail="missing")

    @app.get("/item/{item_id}")
    async def item(item_id: int) -> dict:
        # Path-int parsing failure -> 422 RequestValidationError, handled
        # by Starlette's ExceptionMiddleware *inside* our middleware.
        return {"item_id": item_id}

    @app.get("/boom")
    async def boom() -> dict:
        # Unhandled exception -> escapes ExceptionMiddleware, caught by
        # the outer ServerErrorMiddleware *outside* our middleware.
        raise RuntimeError("kaboom")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> asyncio.AsyncIterator[bytes]:
            # Correlation context must still be bound when body chunks
            # are generated (the BaseHTTPMiddleware streaming hazard).
            for _ in range(3):
                payload = ctx.get_correlation_id()
                yield f"{payload}\n".encode()

        return StreamingResponse(gen(), media_type="text/plain")

    return app


@pytest.fixture
async def client():
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def error_client():
    app = _build_error_app()
    # raise_app_exceptions=False so the test can inspect the synthesized
    # 500 body/headers even when an exception propagates to the transport.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestIncomingHeader:
    @pytest.mark.asyncio
    async def test_header_propagates_into_context(self, client: AsyncClient):
        cid = "abc-123-from-client"
        resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
        assert resp.status_code == 200
        assert resp.json()["correlation_id"] == cid

    @pytest.mark.asyncio
    async def test_response_echoes_header(self, client: AsyncClient):
        cid = "abc-123"
        resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
        assert resp.headers["X-Correlation-Id"] == cid


class TestGenerateWhenMissing:
    @pytest.mark.asyncio
    async def test_generates_uuid_when_header_missing(self, client: AsyncClient):
        resp = await client.get("/echo")
        body_cid = resp.json()["correlation_id"]
        assert body_cid is not None
        uuid.UUID(body_cid)
        assert resp.headers["X-Correlation-Id"] == body_cid


class TestRequestId:
    @pytest.mark.asyncio
    async def test_each_request_gets_distinct_request_id(self, client: AsyncClient):
        a = (await client.get("/echo")).json()["request_id"]
        b = (await client.get("/echo")).json()["request_id"]
        assert a is not None
        assert b is not None
        assert a != b


class TestContextLeak:
    @pytest.mark.asyncio
    async def test_context_cleared_after_response(self, client: AsyncClient):
        await client.get("/echo", headers={"X-Correlation-Id": "leak-check-abc-123"})
        # Each ASGI request runs in its own task; outer test scope must
        # never inherit the request-scoped correlation id.
        assert ctx.get_correlation_id() is None


class TestConcurrentRequests:
    @pytest.mark.asyncio
    async def test_concurrent_requests_keep_isolated_ids(self, client: AsyncClient):
        async def fire(cid: str) -> str:
            resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
            return resp.json()["correlation_id"]

        ids = ("alpha-correlation-id", "beta-correlation-id")
        results = await asyncio.gather(*(fire(c) for c in ids))
        assert tuple(results) == ids


class TestConcurrentIsolation:
    """Fire N simultaneous requests through the real middleware and verify
    each response carries exactly its own correlation id — no
    cross-contamination in the response headers, the in-request
    observability context, or the per-request ``request_id``."""

    @pytest.mark.asyncio
    async def test_many_concurrent_requests_stay_isolated(self, client: AsyncClient):
        n = 32
        sent_ids = [f"cid-{i:03d}-distinct" for i in range(n)]

        async def fire(cid: str) -> tuple[str, str, str]:
            resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
            assert resp.status_code == 200
            body = resp.json()
            return (
                resp.headers["X-Correlation-Id"],
                body["correlation_id"],
                body["request_id"],
            )

        results = await asyncio.gather(*(fire(c) for c in sent_ids))

        header_ids = [hdr for hdr, _, _ in results]
        body_ids = [bid for _, bid, _ in results]
        request_ids = [rid for _, _, rid in results]

        # Header + body must echo back exactly the id each client sent, in
        # order, with no scrambling between concurrent requests.
        assert header_ids == sent_ids
        assert body_ids == sent_ids
        # Per-request ids are all distinct (no sharing across requests).
        assert len(set(request_ids)) == n
        # Nothing leaked into the outer test task's context.
        assert ctx.get_correlation_id() is None
        assert structlog.contextvars.get_contextvars() == {}


class TestSafeCorrelationId:
    """Unit tests for the public ``safe_correlation_id`` validator."""

    def test_valid_id_is_preserved(self):
        assert safe_correlation_id("abc-123") == "abc-123"

    def test_none_generates_uuid(self):
        out = safe_correlation_id(None)
        uuid.UUID(out)  # raises if not a valid uuid
        assert out != ""

    def test_empty_string_generates_uuid(self):
        out = safe_correlation_id("")
        uuid.UUID(out)

    def test_returns_fresh_uuid_for_invalid_each_call(self):
        # Different calls -> different ids (not cached).
        assert safe_correlation_id(None) != safe_correlation_id(None)

    def test_crlf_in_header_is_rejected(self):
        out = safe_correlation_id("legit\r\nSet-Cookie: pwn=1")
        assert "\r" not in out
        assert "\n" not in out
        assert "Set-Cookie" not in out

    def test_oversized_header_is_replaced(self):
        out = safe_correlation_id("x" * 10_000)
        assert len(out) <= 128

    def test_control_chars_rejected(self):
        out = safe_correlation_id("\x1b[31mred")
        assert "\x1b" not in out

    def test_nonascii_rejected(self):
        # \u00e9 is 'é' — outside visible-ASCII range.
        out = safe_correlation_id("café-123")
        assert "é" not in out
        uuid.UUID(out)

    def test_space_rejected(self):
        # Space is \x20, just below the \x21 lower bound — must be rejected.
        out = safe_correlation_id("has space")
        assert " " not in out

    def test_del_rejected(self):
        # DEL (\x7f) is above the \x7e upper bound — must be rejected.
        out = safe_correlation_id("bad\x7fid")
        assert "\x7f" not in out

    def test_tilde_at_upper_bound_accepted(self):
        # \x7e ('~') is the highest allowed character.
        assert safe_correlation_id("id~") == "id~"

    def test_bang_at_lower_bound_accepted(self):
        # \x21 ('!') is the lowest allowed character.
        assert safe_correlation_id("!id") == "!id"

    def test_backward_compat_alias_exists(self):
        # The private name must remain importable as a thin alias.
        from engine.observability.middleware import _safe_correlation_id

        assert _safe_correlation_id is safe_correlation_id
        assert _safe_correlation_id("ok-1") == "ok-1"


class TestUnsafeHeaderRegeneration:
    """Every unsafe/malformed incoming id must be discarded and replaced
    with a fresh UUID4 — never echoed or partially passed through. These
    payloads could otherwise enable response-splitting (CRLF), header
    smuggling (NUL/control bytes), terminal-control corruption (ANSI
    escapes / vertical tab / form feed), or log injection."""

    # Every entry pairs a human-readable label with a concrete attack
    # payload. The validator must reject *all* of them.
    _UNSAFE: tuple[tuple[str, str], ...] = (
        ("crlf", "legit\r\nSet-Cookie: pwn=1"),
        ("bare_cr", "abc\rdef"),
        ("bare_lf", "abc\ndef"),
        ("nul", "abc\x00def"),
        ("del", "bad\x7fid"),
        ("vertical_tab", "abc\x0bdef"),
        ("form_feed", "abc\x0cdef"),
        ("ansi_red", "\x1b[31mred\x1b[0m"),
        ("ansi_clear_screen", "\x1b[2J\x1b[H"),
        ("ansi_cursor_up", "abc\x1b[1Adef"),
    )

    @pytest.mark.parametrize(("label", "value"), _UNSAFE, ids=[lbl for lbl, _ in _UNSAFE])
    def test_unsafe_value_is_regenerated_to_uuid4(self, label: str, value: str):
        out = safe_correlation_id(value)
        # Must be a genuine UUID4 — never the raw input, never empty.
        parsed = uuid.UUID(out)
        assert parsed.version == 4, f"{label}: expected UUID4, got {out!r}"
        assert out != value, f"{label}: unsafe value was passed through"
        # Must not contain *any* control / CRLF byte from the payload set,
        # even ones this particular value did not use.
        for bad in ("\r", "\n", "\x00", "\x7f", "\x0b", "\x0c", "\x1b"):
            assert bad not in out, f"{label}: output contains {bad!r}: {out!r}"
        # And the whole thing must remain within the documented cap.
        assert len(out) <= 128

    @pytest.mark.asyncio
    async def test_unsafe_value_regenerated_end_to_end(self, client: AsyncClient):
        # Even when delivered as a real request, the CRLF payload must be
        # replaced by a fresh UUID4 on both the header and the bound
        # context — never smuggled through.
        resp = await client.get("/echo", headers={"X-Correlation-Id": "a\r\nb\x00c"})
        out = resp.headers["X-Correlation-Id"]
        parsed = uuid.UUID(out)
        assert parsed.version == 4
        assert resp.json()["correlation_id"] == out


class TestOverlongHeaderRegeneration:
    """Values exceeding the 128-char cap — including multi-KB payloads that
    could be used for memory-exhaustion or log-flooding — must be rejected
    and replaced with a fresh, short UUID4."""

    @pytest.mark.parametrize(
        "size",
        [1025, 10_241],
        ids=["just_over_1kb", "just_over_10kb"],
    )
    def test_overlong_value_is_regenerated_to_uuid4(self, size: int):
        value = "a" * size
        out = safe_correlation_id(value)
        # Regenerated -> a real UUID4 well under the cap.
        parsed = uuid.UUID(out)
        assert parsed.version == 4
        assert len(out) <= 128
        # The giant payload must never survive into the output.
        assert out != value
        assert "a" * 128 not in out


class TestHeaderInjectionDefense:
    @pytest.mark.asyncio
    async def test_crlf_in_header_is_rejected(self, client: AsyncClient):
        # httpx blocks CRLF in raw headers, so we drive a manually
        # constructed value through the validator instead.
        out = safe_correlation_id("legit\r\nSet-Cookie: pwn=1")
        assert "\r" not in out
        assert "\n" not in out
        assert "Set-Cookie" not in out

    @pytest.mark.asyncio
    async def test_oversized_header_is_replaced(self, client: AsyncClient):
        out = safe_correlation_id("x" * 10_000)
        assert len(out) <= 128

    @pytest.mark.asyncio
    async def test_control_chars_rejected(self, client: AsyncClient):
        out = safe_correlation_id("\x1b[31mred")
        assert "\x1b" not in out


class TestNonHttpPassthrough:
    """Non-http scopes (lifespan / websocket) must delegate unchanged."""

    @pytest.mark.asyncio
    async def test_non_http_scope_delegates_without_binding(self):
        called = {"downstream": False}

        async def downstream(scope, receive, send):
            called["downstream"] = True

        mw = CorrelationIdMiddleware(downstream)
        await mw({"type": "lifespan"}, _noop_receive, _noop_send)
        assert called["downstream"] is True
        # No request-scoped context should have been bound.
        assert ctx.get_correlation_id() is None


async def _noop_receive():
    return {"type": "http.disconnect"}


async def _noop_send(message):
    return None


class TestErrorHeaderPropagation:
    """The correlation header must appear on *every* response — including
    client errors (HTTPException / validation) and server errors (unhandled
    exceptions caught by the outer ServerErrorMiddleware)."""

    @pytest.mark.asyncio
    async def test_http_exception_response_has_header(self, error_client: AsyncClient):
        resp = await error_client.get("/http-error")
        assert resp.status_code == 404
        assert "X-Correlation-Id" in resp.headers

    @pytest.mark.asyncio
    async def test_http_exception_echoes_client_supplied_id(self, error_client: AsyncClient):
        cid = "client-supplied-404"
        resp = await error_client.get("/http-error", headers={"X-Correlation-Id": cid})
        assert resp.status_code == 404
        assert resp.headers["X-Correlation-Id"] == cid

    @pytest.mark.asyncio
    async def test_validation_error_response_has_header(self, error_client: AsyncClient):
        # Non-int path param -> 422 RequestValidationError.
        resp = await error_client.get("/item/not-a-number")
        assert resp.status_code == 422
        assert "X-Correlation-Id" in resp.headers

    @pytest.mark.asyncio
    async def test_unhandled_exception_response_has_header(self, error_client: AsyncClient):
        # Unhandled RuntimeError escapes ExceptionMiddleware; our middleware
        # must synthesize a 500 carrying the correlation header.
        resp = await error_client.get("/boom")
        assert resp.status_code == 500
        assert "X-Correlation-Id" in resp.headers

    @pytest.mark.asyncio
    async def test_unhandled_exception_echoes_client_supplied_id(self, error_client: AsyncClient):
        cid = "trace-this-500"
        resp = await error_client.get("/boom", headers={"X-Correlation-Id": cid})
        assert resp.status_code == 500
        assert resp.headers["X-Correlation-Id"] == cid

    @pytest.mark.asyncio
    async def test_unhandled_exception_header_is_valid_uuid_when_absent(
        self, error_client: AsyncClient
    ):
        resp = await error_client.get("/boom")
        uuid.UUID(resp.headers["X-Correlation-Id"])

    @pytest.mark.asyncio
    async def test_unhandled_exception_body_is_json(self, error_client: AsyncClient):
        resp = await error_client.get("/boom")
        assert resp.json() == {"detail": "Internal Server Error"}


class TestStreamingHeaderPropagation:
    """Streaming responses must (a) carry the header and (b) still see the
    bound correlation context while body chunks are generated — the exact
    scenario where ``BaseHTTPMiddleware`` resets context too early."""

    @pytest.mark.asyncio
    async def test_streaming_response_has_header(self, error_client: AsyncClient):
        resp = await error_client.get("/stream")
        assert resp.status_code == 200
        assert "X-Correlation-Id" in resp.headers

    @pytest.mark.asyncio
    async def test_streaming_echoes_client_supplied_id(self, error_client: AsyncClient):
        cid = "streaming-cid-abc"
        resp = await error_client.get("/stream", headers={"X-Correlation-Id": cid})
        assert resp.headers["X-Correlation-Id"] == cid

    @pytest.mark.asyncio
    async def test_context_visible_during_body_generation(self, error_client: AsyncClient):
        # Each yielded line is the correlation id observed *inside* the
        # streaming generator. If context leaked-reset early (the
        # BaseHTTPMiddleware hazard) these would be empty/"None".
        cid = "ctx-during-stream"
        resp = await error_client.get("/stream", headers={"X-Correlation-Id": cid})
        lines = resp.text.strip().split("\n")
        assert len(lines) == 3
        assert all(line == cid for line in lines)


class TestExceptionPathContextReset:
    @pytest.mark.asyncio
    async def test_context_reset_after_unhandled_exception(self, error_client: AsyncClient):
        # The finally block must reset tokens even when an exception
        # propagates, otherwise the outer test scope would inherit the id.
        await error_client.get("/boom", headers={"X-Correlation-Id": "should-not-leak"})
        assert ctx.get_correlation_id() is None

    @pytest.mark.asyncio
    async def test_context_reset_after_http_exception(self, error_client: AsyncClient):
        await error_client.get("/http-error", headers={"X-Correlation-Id": "should-not-leak"})
        assert ctx.get_correlation_id() is None
