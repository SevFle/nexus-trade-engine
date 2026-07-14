"""Comprehensive tests for ``engine.api.body_size_limit.BodySizeLimitMiddleware``.

Why a dedicated, raw-ASGI test file
-----------------------------------
The middleware enforces a request-body cap on two paths:

1. **Fast path** — an honest ``Content-Length`` header over the cap is
   rejected before a single byte is read.
2. **Byte-counting path** — the ASGI ``receive`` callable is wrapped so a
   lying / chunked client is still capped at the first chunk that crosses
   the limit.

The fast path parses ``Content-Length`` with ``int(value)`` where ``value``
is the *raw header bytes* straight off the ASGI scope. The security-critical
question is therefore: **what happens when those bytes are malicious?**

That question cannot be answered through Starlette's ``TestClient`` /
httpx: httpx validates outgoing headers and *refuses* to transmit C0
control characters or ``\\r\\n`` sequences. Any attempt to deliver a
``Content-Length: 5\\r\\nX-Injected: evil`` value through the test client is
rejected by httpx long before the middleware ever sees it. The existing
``tests/test_client_errors.py::TestBodySizeCap`` only proves "a 1.5 MiB body
is rejected" — it cannot reach the parser with hostile bytes.

These tests therefore drive the middleware as a **raw ASGI application** by
hand-building the ``scope`` dict. That lets us inject arbitrary header bytes
and verify the parser degrades safely. Boundary sizes are expressed
**relative to the configured cap** (``CAP ± delta``) rather than as arbitrary
``1 KiB`` / ``10 KiB`` constants, and the C0 control-character set is
generated programmatically so no dangerous codepoint is forgotten.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api.body_size_limit import (
    BodySizeLimitExceededError,
    BodySizeLimitMiddleware,
)
from engine.app import create_app

# A small, deterministic cap. Every size assertion below is derived from
# this value (``CAP - 1``, ``CAP``, ``CAP + 1``, ...) so the tests are
# cap-relative boundary tests, not arbitrary "1 KiB / 10 KiB" magic numbers.
CAP = 1024


# ---------------------------------------------------------------------------
# Raw-ASGI harness helpers
# ---------------------------------------------------------------------------


def _http_scope(
    *,
    method: str = "POST",
    path: str = "/ingest",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    """Build a minimal but well-formed HTTP ASGI scope.

    ``headers`` is taken verbatim — including any bytes that httpx would
    refuse to send — so we can probe the middleware's parser directly.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(k, v) for k, v in (headers or [])],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _receive_from_body(chunks: list[bytes]):
    """Build an async ``receive`` callable that streams ``http.request``
    messages, one per chunk. The final message carries ``more_body=False``."""
    if not chunks:
        messages: list[dict[str, Any]] = [
            {"type": "http.request", "body": b"", "more_body": False}
        ]
    else:
        last = len(chunks) - 1
        messages = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": i < last,
            }
            for i, chunk in enumerate(chunks)
        ]
    queue = list(messages)

    async def receive() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        # Exhausted: ASGI clients must keep answering until disconnect.
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


class _Recorder:
    """Inner ASGI app that drains the request body through ``receive()``
    (exercising the middleware's wrapped receive) then emits a response.

    Captures the observed body, any exception that propagated up through
    ``receive`` (e.g. ``BodySizeLimitExceededError``), and how many times
    ``receive`` was consulted — so tests can distinguish the fast path
    (body never read) from the byte-counting path.
    """

    def __init__(self, status: int = 200, resp: bytes = b"ok") -> None:
        self.status = status
        self.resp = resp
        self.observed_body = b""
        self.received_error: BaseException | None = None
        self.receive_calls = 0

    async def __call__(self, scope, receive, send) -> None:
        try:
            while True:
                self.receive_calls += 1
                msg = await receive()
                mtype = msg.get("type")
                if mtype == "http.request":
                    self.observed_body += msg.get("body", b"")
                    if not msg.get("more_body"):
                        break
                else:
                    break
        except BaseException as exc:  # BodySizeLimitExceededError lands here
            # Record for assertions, then *re-raise* so the middleware's
            # ``except BodySizeLimitExceededError`` boundary can translate
            # it into a 413 — exactly as it propagates uncaught through a
            # real FastAPI route handler that reads ``await request.body()``.
            self.received_error = exc
            raise
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(self.resp)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": self.resp})


class _SentMessages:
    """Capture callable for the ASGI ``send`` channel with convenience views."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m.get("type") == "http.response.body"
        )

    @property
    def response_headers(self) -> list[tuple[bytes, bytes]]:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return list(m.get("headers", []))
        return []


async def _drive(
    middleware: BodySizeLimitMiddleware,
    inner: _Recorder,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    chunks: list[bytes] | None = None,
    method: str = "POST",
) -> tuple[_SentMessages, _Recorder]:
    send = _SentMessages()
    await middleware(
        _http_scope(method=method, headers=headers),
        _receive_from_body(list(chunks or [])),
        send,
    )
    return send, inner


# ---------------------------------------------------------------------------
# 1. Construction & non-HTTP pass-through
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_zero_max_bytes_rejected(self):
        with pytest.raises(ValueError):
            BodySizeLimitMiddleware(_Recorder(), max_bytes=0)

    def test_negative_max_bytes_rejected(self):
        with pytest.raises(ValueError):
            BodySizeLimitMiddleware(_Recorder(), max_bytes=-5)

    def test_positive_max_bytes_stored(self):
        m = BodySizeLimitMiddleware(_Recorder(), max_bytes=42)
        assert m.max_bytes == 42

    async def test_non_http_scope_passes_through_untouched(self):
        """lifespan / websocket scopes must bypass the body cap entirely."""
        captured: dict[str, Any] = {}

        async def inner(scope, receive, send):
            captured["type"] = scope["type"]
            await send({"type": "lifespan.startup.complete"})

        middleware = BodySizeLimitMiddleware(inner, max_bytes=CAP)
        send = _SentMessages()
        await middleware(
            {"type": "lifespan", "asgi": {"version": "3.0"}},
            _receive_from_body([]),
            send,
        )
        assert captured["type"] == "lifespan"
        # No 413 was synthesised for a non-http scope.
        assert send.status is None


# ---------------------------------------------------------------------------
# 2. Cap-relative boundary tests (replace arbitrary 1 KiB / 10 KiB sizes)
# ---------------------------------------------------------------------------


class TestCapRelativeBoundaries:
    @pytest.mark.parametrize(("delta", "expect_413"), [(-1, False), (0, False), (+1, True)])
    async def test_declared_content_length_boundary(self, delta, expect_413):
        """Fast path: ``declared == CAP`` passes, ``CAP + 1`` is rejected."""
        size = CAP + delta
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", str(size).encode("ascii"))],
            chunks=[b"x" * size],
        )
        if expect_413:
            assert send.status == 413
        else:
            assert send.status == 200
            assert len(inner.observed_body) == size

    @pytest.mark.parametrize(("delta", "expect_413"), [(0, False), (+1, True)])
    async def test_byte_counting_boundary_without_content_length(self, delta, expect_413):
        """No Content-Length header → pure byte-counting path at the cap edge."""
        size = CAP + delta
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            chunks=[b"a" * size],
        )
        if expect_413:
            assert send.status == 413
        else:
            assert send.status == 200
            assert len(inner.observed_body) == size

    async def test_exact_cap_accepted_single_chunk(self):
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            chunks=[b"a" * CAP],
        )
        assert send.status == 200
        assert len(inner.observed_body) == CAP

    async def test_exact_cap_accepted_split_across_chunks(self):
        """``CAP`` bytes split into many chunks must still be accepted."""
        inner = _Recorder()
        chunk = b"a" * 16
        n_chunks = CAP // len(chunk)
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            chunks=[chunk] * n_chunks,
        )
        assert send.status == 200
        assert len(inner.observed_body) == n_chunks * len(chunk) == CAP

    async def test_oversize_detected_across_multiple_chunks(self):
        """Running total crossing the cap mid-stream triggers a 413."""
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            chunks=[b"a" * (CAP - 1), b"bbb"],
        )
        assert send.status == 413
        assert isinstance(inner.received_error, BodySizeLimitExceededError)

    async def test_lying_client_small_content_length_large_body_rejected(self):
        """Declared length is tiny but the real body exceeds the cap: the
        byte-counting path must still reject — the cap cannot be lied past."""
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", b"1")],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send.status == 413
        assert isinstance(inner.received_error, BodySizeLimitExceededError)

    async def test_fast_path_rejects_without_reading_body(self):
        """A declared-oversize body is 413'd on the fast path, so ``receive``
        is never consulted and the inner app never runs."""
        inner = _Recorder()
        oversize = CAP + 1000
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", str(oversize).encode("ascii"))],
            chunks=[b"x" * oversize],
        )
        assert send.status == 413
        assert inner.receive_calls == 0
        assert inner.observed_body == b""

    async def test_huge_numeric_content_length_rejected_on_fast_path(self):
        """Python ``int`` has no overflow; an absurd declared length is a
        clean integer and trips the fast path without reading the body."""
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", b"99999999999999999999")],
            chunks=[b"tiny"],
        )
        assert send.status == 413
        assert inner.receive_calls == 0


# ---------------------------------------------------------------------------
# 3. Raw-ASGI malicious-header tests (bypass httpx validation)
# ---------------------------------------------------------------------------


class TestRawScopeMaliciousHeaders:
    """httpx refuses to emit C0 / CRLF bytes in headers, so these payloads
    can only be delivered by hand-crafting the ASGI scope. They verify the
    ``Content-Length`` parser cannot be tricked into a false reject, a bypass,
    or a header-injection echo."""

    async def test_crlf_in_content_length_no_false_reject_and_no_bypass(self):
        middleware = BodySizeLimitMiddleware(_Recorder(), max_bytes=CAP)

        # A CRLF-laced value cannot be int-parsed (declared sentinel -1):
        # a small body must NOT be falsely 413'd.
        inner_small = _Recorder()
        send_small, _ = await _drive(
            middleware,
            inner_small,
            headers=[(b"content-length", b"5\r\nX-Injected: evil")],
            chunks=[b"abcde"],
        )
        assert send_small.status == 200, "malformed content-length must not cause a false 413"

        # ...and a body that genuinely exceeds the cap must STILL be 413'd
        # via byte counting — the malformed header must not bypass the cap.
        inner_large = _Recorder()
        send_large, _ = await _drive(
            middleware,
            inner_large,
            headers=[(b"content-length", b"5\r\nX-Injected: evil")],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send_large.status == 413, "malformed content-length must not bypass the byte cap"

    async def test_smuggled_header_fragment_is_not_echoed_in_413(self):
        """Even with a header-looking fragment smuggled into Content-Length,
        the synthesised 413 response carries only the middleware's own
        headers — nothing the attacker injected."""
        inner = _Recorder()
        send, _ = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", b"999999\r\nX-Evil: pwned")],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send.status == 413
        names = {name for name, _ in send.response_headers}
        assert names == {b"content-type", b"content-length"}
        assert b"request_body_too_large" in send.body

    async def test_nul_byte_in_content_length_is_safe(self):
        middleware = BodySizeLimitMiddleware(_Recorder(), max_bytes=CAP)

        inner_small = _Recorder()
        send_small, _ = await _drive(
            middleware,
            inner_small,
            headers=[(b"content-length", b"5\x00999999")],
            chunks=[b"abcde"],
        )
        assert send_small.status == 200

        inner_large = _Recorder()
        send_large, _ = await _drive(
            middleware,
            inner_large,
            headers=[(b"content-length", b"5\x00999999")],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send_large.status == 413

    async def test_duplicate_content_length_first_match_wins_and_cap_holds(self):
        """The middleware breaks on the first Content-Length it finds. A later,
        larger duplicate cannot force a fast-path reject, and byte counting
        still protects the cap."""
        inner = _Recorder()
        send, _ = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[
                (b"content-length", b"1"),
                (b"content-length", str(CAP + 1000).encode("ascii")),
            ],
            chunks=[b"x" * (CAP + 1)],
        )
        # First header (1) passes the fast path; CAP+1 real body is rejected
        # by byte counting.
        assert send.status == 413

    async def test_invalid_content_length_falls_through_to_byte_counting(self):
        """Non-numeric declared value → sentinel -1 → neither fast-reject nor
        crash; the request is governed purely by the byte counter."""
        middleware = BodySizeLimitMiddleware(_Recorder(), max_bytes=CAP)

        inner_ok = _Recorder()
        send_ok, _ = await _drive(
            middleware,
            inner_ok,
            headers=[(b"content-length", b"not-a-number")],
            chunks=[b"fine"],
        )
        assert send_ok.status == 200

        inner_big = _Recorder()
        send_big, _ = await _drive(
            middleware,
            inner_big,
            headers=[(b"content-length", b"not-a-number")],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send_big.status == 413


# ---------------------------------------------------------------------------
# 4. C0 control characters — generated programmatically
# ---------------------------------------------------------------------------

# Programmatic C0 set (0x00-0x1F) excluding horizontal tab, per the task
# spec. Each value is wrapped as b"a" + control + b"b" so it is resolutely
# non-numeric and therefore guaranteed to fail int() parsing, no matter how
# lenient the locale / Python build.
C0_CONTROL_CHARS: list[tuple[str, bytes]] = [
    (f"ctrl_{c:02x}", b"a" + bytes([c]) + b"b") for c in range(0x20) if chr(c) not in "\t"
]

# Named attack vectors beyond the bare C0 range.
NAMED_ATTACKS: list[tuple[str, bytes]] = [
    ("crlf", b"a\r\nb"),
    ("nul", b"a\x00b"),
    ("del_0x7f", b"a\x7fb"),
    ("rtl_u202e", "a\u202eb".encode("utf-8")),  # RIGHT-TO-LEFT OVERRIDE
    ("zero_width_u200b", "a\u200bb".encode("utf-8")),  # ZERO WIDTH SPACE
]


class TestControlCharContentLength:
    """Every control character smuggled into a Content-Length value must
    make ``int()`` raise (→ declared sentinel), so the middleware never
    crashes and never returns a spurious 413 — and the cap is never
    bypassed."""

    @pytest.mark.parametrize(
        ("name", "value"), C0_CONTROL_CHARS, ids=[n for n, _ in C0_CONTROL_CHARS]
    )
    async def test_c0_control_char_no_false_reject(self, name, value):
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", value)],
            chunks=[b"ok"],
        )
        assert send.status == 200, f"{name}: control char must not trigger a false 413"
        assert inner.received_error is None, f"{name}: control char must not raise into the app"

    @pytest.mark.parametrize(
        ("name", "value"), C0_CONTROL_CHARS, ids=[n for n, _ in C0_CONTROL_CHARS]
    )
    async def test_c0_control_char_does_not_bypass_cap(self, name, value):
        inner = _Recorder()
        send, _ = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", value)],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send.status == 413, f"{name}: control char must not bypass the byte cap"

    @pytest.mark.parametrize(("name", "value"), NAMED_ATTACKS, ids=[n for n, _ in NAMED_ATTACKS])
    async def test_named_attack_vector_safe_small_and_large(self, name, value):
        middleware = BodySizeLimitMiddleware(_Recorder(), max_bytes=CAP)

        inner_small = _Recorder()
        send_small, _ = await _drive(
            middleware,
            inner_small,
            headers=[(b"content-length", value)],
            chunks=[b"ok"],
        )
        assert send_small.status == 200, f"{name}: must not cause a false 413"

        inner_large = _Recorder()
        send_large, _ = await _drive(
            middleware,
            inner_large,
            headers=[(b"content-length", value)],
            chunks=[b"x" * (CAP + 1)],
        )
        assert send_large.status == 413, f"{name}: must not bypass the cap"


class TestIntParsingWhitespaceEdgeCase:
    """Documents that Python's ``int()`` strips ASCII-whitespace control
    characters (``\\t \\n \\r \\f \\v``) from a *numeric* value. The middleware
    must still treat the resulting integer correctly — no crash, and the
    correct cap decision for both the under- and over-cap cases."""

    WS_CONTROL = (
        pytest.param(0x09, b"\t", id="tab_09"),
        pytest.param(0x0A, b"\n", id="lf_0a"),
        pytest.param(0x0B, b"\x0b", id="vtab_0b"),
        pytest.param(0x0C, b"\x0c", id="ff_0c"),
        pytest.param(0x0D, b"\r", id="cr_0d"),
    )

    @pytest.mark.parametrize(("codept", "rep"), WS_CONTROL)
    async def test_whitespace_wrapping_valid_number_parses_and_passes(self, codept, rep):
        value = rep + b"100"
        # Sanity: the documented lenient int() behaviour.
        assert int(value) == 100
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", value)],
            chunks=[b"x" * 100],
        )
        assert send.status == 200
        assert len(inner.observed_body) == 100

    async def test_whitespace_wrapping_oversize_number_rejected_on_fast_path(self):
        value = b"\t" + str(CAP + 1).encode("ascii")
        assert int(value) == CAP + 1  # parses despite the leading tab
        inner = _Recorder()
        send, inner = await _drive(
            BodySizeLimitMiddleware(inner, max_bytes=CAP),
            inner,
            headers=[(b"content-length", value)],
            chunks=[b"tiny"],
        )
        assert send.status == 413
        assert inner.receive_calls == 0  # fast path, body never read


# ---------------------------------------------------------------------------
# 5. Exception-to-response translation & end-to-end production wiring
# ---------------------------------------------------------------------------


class TestExceptionTranslation:
    async def test_body_size_exceeded_raised_by_inner_app_converts_to_413(self):
        """The except clause must translate BodySizeLimitExceededError — even
        when raised directly by the inner app rather than the receive
        wrapper — into a 413 response."""

        async def evil_inner(scope, receive, send):
            raise BodySizeLimitExceededError

        middleware = BodySizeLimitMiddleware(evil_inner, max_bytes=CAP)
        send = _SentMessages()
        await middleware(
            _http_scope(),
            _receive_from_body([b"x"]),
            send,
        )
        assert send.status == 413
        assert b"request_body_too_large" in send.body


class TestProductionWiringBoundary:
    """End-to-end boundary test at the *real* production cap (1 MiB) wired in
    ``engine.app.create_app``. Sizes are expressed relative to that cap."""

    PROD_CAP = 1_048_576

    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(create_app())

    def test_body_at_exactly_cap_is_not_413(self, client: TestClient):
        """Exactly ``CAP`` bytes passes the middleware (declared == cap is not
        strictly greater; byte total == cap is not strictly greater) and
        reaches the route handler, which 422's on the non-JSON body."""
        body = b"x" * self.PROD_CAP
        r = client.post(
            "/api/v1/client/errors",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert r.status_code != 413, "body at exactly the cap must reach the app"
        assert r.status_code == 422

    def test_body_one_byte_over_cap_is_413(self, client: TestClient):
        body = b"x" * (self.PROD_CAP + 1)
        r = client.post(
            "/api/v1/client/errors",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413
        assert r.json() == {"error": "request_body_too_large"}
