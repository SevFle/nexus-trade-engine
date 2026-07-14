"""Unit tests for the X-Correlation-Id middleware.

Verifies the four contract guarantees of the correlation-id middleware:

1. **Auto-generation** -- a fresh UUID4 is minted when the client omits
   ``X-Correlation-ID``.
2. **Propagation** -- a client-supplied (safe) id is preserved end-to-end.
3. **Response header** -- the id is echoed back on every response.
4. **structlog context binding** -- the id is bound to the logging context
   so every record emitted while handling the request carries it.

Both shipped implementations are exercised:

* :class:`engine.middleware.CorrelationIdMiddleware` -- the raw-ASGI variant
  registered by the app factory (:func:`engine.app.create_app`). Binds the
  id to the observability :mod:`contextvars` which the ``add_correlation_context``
  processor merges into structlog records.
* :class:`engine.middleware.correlation.BaseHTTPCorrelationIdMiddleware` --
  the ``BaseHTTPMiddleware`` variant that additionally binds the id directly
  to :mod:`structlog.contextvars`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.middleware import correlation as corr_module
from engine.middleware.correlation import (
    CORRELATION_HEADER,
    BaseHTTPCorrelationIdMiddleware,
)
from engine.observability import context as ctx
from engine.observability.middleware import (
    CorrelationIdMiddleware as RawAsgiCorrelationIdMiddleware,
)
from engine.observability.processors import add_correlation_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

# The header is conceptually ``X-Correlation-ID`` (the task spec) but HTTP
# headers are case-insensitive; the canonical name used throughout the
# codebase is ``X-Correlation-Id``. We assert the constants agree and use
# the canonical name for lookups.
assert CORRELATION_HEADER.lower() == "x-correlation-id"

#: The two middleware implementations under test, keyed by a human label so
#: parametrized test ids stay readable. Both must satisfy the same contract.
MIDDLEWARE_FACTORIES: dict[str, Callable[[FastAPI], None]] = {
    "raw-asgi": lambda app: app.add_middleware(RawAsgiCorrelationIdMiddleware),
    "base-http": lambda app: app.add_middleware(BaseHTTPCorrelationIdMiddleware),
}


def _merged_structlog_event(event: str = "request.handled") -> dict[str, Any]:
    """Run a fresh event dict through the production correlation processors.

    Mirrors what structlog does in :func:`engine.observability.logging.setup_logging`
    (``merge_contextvars`` then ``add_correlation_context``) without depending
    on global structlog configuration being applied. This is the faithful
    way to assert "the correlation id reaches structlog log records".
    """
    event_dict: dict[str, Any] = {"event": event}
    return add_correlation_context(
        None, "info", structlog.contextvars.merge_contextvars(None, "info", event_dict)
    )


def _build_app(register: Callable[[FastAPI], None]) -> FastAPI:
    """Build a minimal FastAPI app whose route captures the correlation
    context as observed *during* request handling.

    The route records four things into ``app.state.observed``:

    * ``correlation_id`` -- from the observability context (the API the rest
      of the codebase reads, e.g. outbound HTTP client).
    * ``structlog_contextvars`` -- a copy of :func:`structlog.contextvars.get_contextvars`
      (the literal ``structlog.contextvars`` binding used by the BaseHTTP
      variant).
    * ``structlog_event`` -- an event dict run through the production
      correlation processors; this is what a structlog log record would
      carry for this request.
    * ``bound_logger_context`` -- the context a freshly obtained structlog
      logger sees via :func:`get_correlation_id`/snapshot.
    """
    app = FastAPI()
    register(app)
    app.state.observed: list[dict[str, Any]] = []

    @app.get("/observe")
    async def observe() -> dict[str, Any]:
        app.state.observed.append(
            {
                "correlation_id": ctx.get_correlation_id(),
                "request_id": ctx.get_request_id(),
                "structlog_contextvars": dict(structlog.contextvars.get_contextvars()),
                "structlog_event": _merged_structlog_event(),
                "context_snapshot": ctx.snapshot(),
            }
        )
        return {"ok": True}

    @app.get("/echo-id")
    async def echo_id() -> dict[str, Any]:
        # Plain route with no context capture, used for header-only checks.
        return {"correlation_id": ctx.get_correlation_id()}

    return app


@pytest.fixture(params=list(MIDDLEWARE_FACTORIES), ids=list(MIDDLEWARE_FACTORIES))
async def client(request) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    register = MIDDLEWARE_FACTORIES[request.param]
    app = _build_app(register)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, app


def _reset_logging_context() -> None:
    """Ensure no correlation state leaks between tests."""
    structlog.contextvars.clear_contextvars()
    ctx.clear_context()


@pytest.fixture(autouse=True)
def _isolate_logging_context():
    _reset_logging_context()
    yield
    _reset_logging_context()


# ---------------------------------------------------------------------------
# 1. Auto-generation when the header is absent
# ---------------------------------------------------------------------------
class TestAutoGenerationWhenAbsent:
    @pytest.mark.asyncio
    async def test_response_header_is_valid_uuid4_when_absent(self, client):
        ac, _ = client
        resp = await ac.get("/echo-id")
        assert resp.status_code == 200
        cid = resp.headers[CORRELATION_HEADER]
        # Must be a valid UUID4 string (the generated form).
        parsed = uuid.UUID(cid)
        assert parsed.version == 4

    @pytest.mark.asyncio
    async def test_generated_id_is_bound_to_context(self, client):
        ac, app = client
        resp = await ac.get("/observe")
        assert resp.status_code == 200
        observed = app.state.observed[-1]
        cid = resp.headers[CORRELATION_HEADER]
        # The id bound during the request matches the one on the response.
        assert observed["correlation_id"] == cid
        assert uuid.UUID(cid).version == 4

    @pytest.mark.asyncio
    async def test_two_requests_without_header_get_distinct_ids(self, client):
        ac, _ = client
        first = await ac.get("/echo-id")
        second = await ac.get("/echo-id")
        cid_a = first.headers[CORRELATION_HEADER]
        cid_b = second.headers[CORRELATION_HEADER]
        assert cid_a != cid_b
        assert uuid.UUID(cid_a).version == 4
        assert uuid.UUID(cid_b).version == 4


# ---------------------------------------------------------------------------
# 2. Propagation when an incoming id is present (and safe)
# ---------------------------------------------------------------------------
class TestPropagationWhenPresent:
    @pytest.mark.asyncio
    async def test_incoming_safe_id_is_echoed_in_response(self, client):
        ac, _ = client
        cid = "client-supplied-trace-abc-123"
        resp = await ac.get("/echo-id", headers={CORRELATION_HEADER: cid})
        assert resp.status_code == 200
        assert resp.headers[CORRELATION_HEADER] == cid

    @pytest.mark.asyncio
    async def test_incoming_safe_id_is_preserved_in_context(self, client):
        ac, app = client
        cid = "propagate-me-456"
        resp = await ac.get("/observe", headers={CORRELATION_HEADER: cid})
        assert resp.status_code == 200
        observed = app.state.observed[-1]
        assert observed["correlation_id"] == cid
        assert resp.headers[CORRELATION_HEADER] == cid

    @pytest.mark.asyncio
    async def test_header_case_insensitive(self, client):
        """``X-Correlation-ID`` and ``X-Correlation-Id`` are the same header."""
        ac, _ = client
        cid = "case-insensitive-789"
        resp = await ac.get("/echo-id", headers={"X-Correlation-ID": cid})
        assert resp.headers[CORRELATION_HEADER] == cid

    @pytest.mark.asyncio
    async def test_unsafe_incoming_id_is_regenerated(self, client):
        """CRLF / control chars must be discarded and a fresh UUID minted."""
        ac, _ = client
        # Visible-ASCII validator rejects a space (0x20 < 0x21).
        resp = await ac.get("/echo-id", headers={CORRELATION_HEADER: "bad id with space"})
        cid = resp.headers[CORRELATION_HEADER]
        assert " " not in cid
        assert uuid.UUID(cid).version == 4


# ---------------------------------------------------------------------------
# 3. Response header presence
# ---------------------------------------------------------------------------
class TestResponseHeaderSet:
    @pytest.mark.asyncio
    async def test_header_present_on_success(self, client):
        ac, _ = client
        resp = await ac.get("/echo-id")
        assert CORRELATION_HEADER in resp.headers

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "register",
        list(MIDDLEWARE_FACTORIES.values()),
        ids=list(MIDDLEWARE_FACTORIES),
    )
    async def test_header_present_on_client_error(self, register):
        """404 (HTTPException) must still carry the header."""
        app = FastAPI()
        register(app)

        from fastapi import HTTPException

        @app.get("/missing")
        async def missing() -> None:
            raise HTTPException(status_code=404, detail="not found")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/missing")
            assert resp.status_code == 404
            assert CORRELATION_HEADER in resp.headers

    @pytest.mark.asyncio
    async def test_request_and_response_ids_match(self, client):
        ac, _ = client
        cid = "round-trip-match-000"
        resp = await ac.get("/echo-id", headers={CORRELATION_HEADER: cid})
        assert resp.headers[CORRELATION_HEADER] == cid

    @pytest.mark.asyncio
    async def test_header_set_for_every_response_in_sequence(self, client):
        ac, _ = client
        for _ in range(5):
            resp = await ac.get("/echo-id")
            assert CORRELATION_HEADER in resp.headers
            uuid.UUID(resp.headers[CORRELATION_HEADER])


# ---------------------------------------------------------------------------
# 4. structlog context binding
# ---------------------------------------------------------------------------
class TestStructlogContextBinding:
    @pytest.mark.asyncio
    async def test_correlation_id_reaches_structlog_event(self, client):
        """The end-to-end guarantee: a structlog log record emitted during a
        request carries the correlation id (via merge_contextvars and/or the
        add_correlation_context processor)."""
        ac, app = client
        cid = "structlog-binding-321"
        resp = await ac.get("/observe", headers={CORRELATION_HEADER: cid})
        assert resp.status_code == 200
        observed = app.state.observed[-1]
        event = observed["structlog_event"]
        assert event["correlation_id"] == cid
        # request_id is also threaded through for per-request granularity.
        assert event["request_id"] is not None

    @pytest.mark.asyncio
    async def test_generated_id_reaches_structlog_event(self, client):
        ac, app = client
        resp = await ac.get("/observe")
        cid = resp.headers[CORRELATION_HEADER]
        observed = app.state.observed[-1]
        assert observed["structlog_event"]["correlation_id"] == cid

    @pytest.mark.asyncio
    async def test_base_http_variant_binds_structlog_contextvars_directly(self):
        """The BaseHTTPMiddleware variant binds the id to
        :mod:`structlog.contextvars` directly (the mechanism the task spec
        describes: "binds it to structlog context via contextvars")."""
        app = _build_app(MIDDLEWARE_FACTORIES["base-http"])
        cid = "direct-contextvar-bind-999"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/observe", headers={CORRELATION_HEADER: cid})
            assert resp.status_code == 200
            observed = app.state.observed[-1]
            cv = observed["structlog_contextvars"]
            assert cv.get("correlation_id") == cid
            assert cv.get("request_id") is not None

    @pytest.mark.asyncio
    async def test_structlog_event_id_matches_response_header(self, client):
        ac, app = client
        resp = await ac.get("/observe")
        cid = resp.headers[CORRELATION_HEADER]
        observed = app.state.observed[-1]
        assert observed["structlog_event"]["correlation_id"] == cid

    @pytest.mark.asyncio
    async def test_context_does_not_leak_between_requests(self, client):
        """After a request completes, the logging/observability context must
        be reset so the next request (or the test harness) doesn't inherit
        a stale correlation id."""
        ac, _ = client
        await ac.get("/echo-id", headers={CORRELATION_HEADER: "must-not-leak-1"})
        # Outside the request scope no id should be visible.
        assert ctx.get_correlation_id() is None
        assert structlog.contextvars.get_contextvars() == {}

    @pytest.mark.asyncio
    async def test_concurrent_requests_keep_isolated_structlog_ids(self, client):
        import asyncio

        ac, _ = client

        async def fire(cid: str) -> str:
            resp = await ac.get("/observe", headers={CORRELATION_HEADER: cid})
            return resp.headers[CORRELATION_HEADER]

        ids = ("alpha-iso", "beta-iso", "gamma-iso")
        results = await asyncio.gather(*(fire(c) for c in ids))
        assert tuple(results) == ids


# ---------------------------------------------------------------------------
# App-factory integration: the middleware is registered and wired up
# ---------------------------------------------------------------------------
class TestAppFactoryRegistration:
    def test_create_app_registers_a_correlation_middleware(self):
        from engine.app import create_app

        app = create_app()
        entry = next(
            (m for m in app.user_middleware if "Correlation" in m.cls.__name__),
            None,
        )
        assert entry is not None, "create_app() must register a correlation middleware"

    def test_engine_middleware_reexports_default(self):
        """``engine.middleware.CorrelationIdMiddleware`` is the raw-ASGI default
        (the one the app factory uses)."""
        import engine.middleware as mw_pkg

        assert mw_pkg.CorrelationIdMiddleware is RawAsgiCorrelationIdMiddleware


# ---------------------------------------------------------------------------
# Module-level sanity: the public API surface the task requires.
# ---------------------------------------------------------------------------
class TestPublicApi:
    def test_correlation_module_exposes_header_constant(self):
        assert corr_module.CORRELATION_HEADER == "X-Correlation-Id"

    def test_correlation_module_exposes_middleware_class(self):
        # The BaseHTTPMiddleware-based implementation lives in the module
        # the task names (engine/middleware/correlation.py).
        assert hasattr(corr_module, "BaseHTTPCorrelationIdMiddleware")

    def test_middleware_classes_callable(self):
        # Both must be constructible around a trivial ASGI app.
        async def app(scope, receive, send):
            return None

        BaseHTTPCorrelationIdMiddleware(app)
        RawAsgiCorrelationIdMiddleware(app)
