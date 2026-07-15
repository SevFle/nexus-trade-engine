"""Tests for the FastAPI correlation middleware."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from engine.observability import context as ctx
from engine.observability.middleware import (
    _HEADER_NAME_BYTES,
    MAX_CORRELATION_ID_LENGTH,
    CorrelationIdMiddleware,
    safe_correlation_id,
)

# The canonical lowercased-bytes header name the middleware matches against
# on ``scope['headers']`` and writes back on the response. Imported directly
# from the middleware module (single source of truth) instead of being
# re-derived here, so the tests can never drift from the middleware's own
# casing/encoding of the header name.


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


class TestSafeCorrelationId:
    """Unit tests for the public ``safe_correlation_id`` validator."""

    def test_valid_id_is_preserved(self):
        assert safe_correlation_id("abc-123") == "abc-123"

    def test_none_generates_uuid(self):
        out = safe_correlation_id(None)
        assert out != ""
        uuid.UUID(out)  # raises if not a valid uuid

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
        assert len(out) <= MAX_CORRELATION_ID_LENGTH

    def test_boundary_length_at_cap_is_accepted(self):
        # Exactly MAX_CORRELATION_ID_LENGTH valid chars is the last length
        # the regex ({1,128}) accepts — it must be preserved verbatim.
        candidate = "a" * MAX_CORRELATION_ID_LENGTH
        out = safe_correlation_id(candidate)
        assert out == candidate
        assert len(out) == MAX_CORRELATION_ID_LENGTH

    def test_boundary_length_cap_plus_one_is_regenerated(self):
        # One char over the cap is the first length the regex rejects — the
        # value must be discarded and a fresh UUID minted instead.
        candidate = "a" * (MAX_CORRELATION_ID_LENGTH + 1)
        out = safe_correlation_id(candidate)
        assert out != candidate
        uuid.UUID(out)
        assert len(out) <= MAX_CORRELATION_ID_LENGTH

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


class TestHeaderInjectionDefenseASGI:
    """Drive the middleware with raw ASGI scopes whose ``scope['headers']``
    carry malicious byte values.

    httpx and Starlette sanitise header *values* before they ever reach the
    ASGI scope, so an httpx-based e2e test can never deliver a CRLF / NUL /
    control byte to the middleware — it is forced to call the validator
    directly, which proves nothing about the middleware's own defence in
    depth and just duplicates the unit tests above (tautological).

    Instead we build the scope dict by hand and inject the exact raw bytes an
    attacker would deliver through a buggy/lenient HTTP parser, or — for the
    shared taskiq path — a crafted Redis label value. The middleware reads
    those bytes off ``scope['headers']`` itself, so this is a genuine
    end-to-end check that every malicious payload is rejected and replaced
    with a clean UUID on both the response header and the bound
    observability context.
    """

    @staticmethod
    async def _drive(header_bytes: bytes | None) -> tuple[str | None, str | None]:
        """Run ``CorrelationIdMiddleware`` against a minimal http scope whose
        ``X-Correlation-Id`` header is set to ``header_bytes`` (raw, so CRLF /
        NUL / control chars survive — httpx would strip them).

        Returns ``(response_header_cid, context_cid)`` — the value the
        middleware actually wrote back on the response header and the value
        it bound to the observability context for the downstream app.
        """
        captured: dict[str, str | None] = {"ctx": None}

        async def downstream(scope, receive, send):
            captured["ctx"] = ctx.get_correlation_id()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        headers = []
        if header_bytes is not None:
            headers.append((_HEADER_NAME_BYTES, header_bytes))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 60000),
            "server": ("test", 80),
        }

        mw = CorrelationIdMiddleware(downstream)
        await mw(scope, receive, send)

        resp_cid: str | None = None
        for message in sent:
            if message["type"] == "http.response.start":
                for name, value in message["headers"]:
                    if name == _HEADER_NAME_BYTES:
                        resp_cid = value.decode("latin-1")
        # The middleware must always emit a correlation header on the
        # response and bind a value to the observability context. Fail
        # loudly here — before any caller does string/uuid work on the
        # result — instead of handing a silent ``None`` downstream.
        ctx_cid = captured["ctx"]
        assert resp_cid is not None
        assert ctx_cid is not None
        return resp_cid, ctx_cid

    @pytest.mark.asyncio
    async def test_crlf_injection_is_sanitized(self):
        # Classic response-splitting / header-smuggling attempt via raw CRLF.
        resp_cid, ctx_cid = await self._drive(b"legit\r\nSet-Cookie: pwn=1")
        assert resp_cid is not None
        assert "\r" not in resp_cid
        assert "\n" not in resp_cid
        assert "Set-Cookie" not in resp_cid
        uuid.UUID(resp_cid)  # a fresh id was minted, not the attacker's prefix
        assert ctx_cid == resp_cid  # response header and bound context agree

    @pytest.mark.asyncio
    async def test_nul_byte_is_sanitized(self):
        resp_cid, ctx_cid = await self._drive(b"bad\x00id")
        assert resp_cid is not None
        assert "\x00" not in resp_cid
        uuid.UUID(resp_cid)
        assert ctx_cid == resp_cid

    @pytest.mark.asyncio
    async def test_ansi_escape_control_chars_are_sanitized(self):
        # ESC [ 31m (terminal colour sequence) plus BEL.
        resp_cid, ctx_cid = await self._drive(b"\x07\x1b[31mred")
        assert resp_cid is not None
        assert "\x07" not in resp_cid
        assert "\x1b" not in resp_cid
        uuid.UUID(resp_cid)
        assert ctx_cid == resp_cid

    @pytest.mark.asyncio
    async def test_all_c0_and_del_control_bytes_are_sanitized(self):
        # Sweep the entire C0 range 0x00-0x1f plus DEL (0x7f). None may leak.
        payload = bytes(range(0x20)) + b"\x7f"
        resp_cid, _ = await self._drive(payload)
        assert resp_cid is not None
        for byte in payload:
            assert chr(byte) not in resp_cid
        uuid.UUID(resp_cid)

    @pytest.mark.asyncio
    async def test_nonascii_bytes_are_sanitized(self):
        # Bytes >= 0x80 are outside the visible-ASCII range the regex allows.
        resp_cid, _ = await self._drive(b"caf\xc3\xa9-\xff")
        assert resp_cid is not None
        assert resp_cid.isascii()
        uuid.UUID(resp_cid)

    @pytest.mark.asyncio
    async def test_space_byte_is_sanitized(self):
        # Space (0x20) sits just below the 0x21 lower bound of the charset.
        resp_cid, _ = await self._drive(b"has space")
        assert resp_cid is not None
        assert " " not in resp_cid
        uuid.UUID(resp_cid)

    @pytest.mark.asyncio
    async def test_oversized_bytes_are_sanitized(self):
        resp_cid, _ = await self._drive(b"x" * 10_000)
        assert resp_cid is not None
        assert len(resp_cid) <= MAX_CORRELATION_ID_LENGTH
        uuid.UUID(resp_cid)

    @pytest.mark.asyncio
    async def test_boundary_length_at_cap_accepted_through_asgi(self):
        candidate = b"a" * MAX_CORRELATION_ID_LENGTH
        resp_cid, ctx_cid = await self._drive(candidate)
        assert resp_cid is not None
        assert resp_cid == candidate.decode()
        assert ctx_cid == candidate.decode()
        assert len(resp_cid) == MAX_CORRELATION_ID_LENGTH

    @pytest.mark.asyncio
    async def test_boundary_length_cap_plus_one_regenerated_through_asgi(self):
        candidate = b"a" * (MAX_CORRELATION_ID_LENGTH + 1)
        resp_cid, _ = await self._drive(candidate)
        assert resp_cid is not None
        assert resp_cid != candidate.decode()
        uuid.UUID(resp_cid)
        assert len(resp_cid) <= MAX_CORRELATION_ID_LENGTH

    @pytest.mark.asyncio
    async def test_valid_header_is_preserved_through_asgi(self):
        # Sanity check: a benign value must round-trip unchanged end-to-end,
        # confirming the sanitiser does not over-reach onto clean input.
        resp_cid, ctx_cid = await self._drive(b"abc-123-from-client")
        assert resp_cid is not None
        assert resp_cid == "abc-123-from-client"
        assert ctx_cid == "abc-123-from-client"


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
