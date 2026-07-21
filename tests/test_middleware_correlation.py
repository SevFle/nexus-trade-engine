"""Tests for the engine.middleware correlation middleware refactor.

Covers the rename of the ``BaseHTTPMiddleware``-based class to
``BaseHTTPCorrelationIdMiddleware`` (with a deprecated alias for one release
cycle), the ``engine.middleware`` package re-exporting the raw-ASGI variant
as the default ``CorrelationIdMiddleware``, and the class-identity assertion
in :func:`engine.app.create_app`.

Also covers request-scoped binding of the correlation context for *both*
middleware builders (raw-ASGI and ``BaseHTTPMiddleware``), and the
StreamingResponse / BackgroundTask scope guarantees of the raw-ASGI
variant.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.middleware.base import BaseHTTPMiddleware

from engine import middleware as mw_pkg
from engine.app import create_app
from engine.middleware import correlation as corr_module
from engine.middleware.correlation import BaseHTTPCorrelationIdMiddleware
from engine.observability import context as ctx
from engine.observability import middleware as obs_middleware
from engine.observability.middleware import (
    CorrelationIdMiddleware,
)

# The two middleware builders covered by the parametrised scope tests.
# Both must produce equivalent request-scoped bindings for ordinary
# (non-streaming, non-background) requests.
_MIDDLEWARE_BUILDERS = [
    pytest.param(CorrelationIdMiddleware, id="raw_asgi"),
    pytest.param(BaseHTTPCorrelationIdMiddleware, id="base_http"),
]


def _reset_structlog_context() -> None:
    """Clear any structlog contextvars left over from a prior test."""
    structlog.contextvars.clear_contextvars()


def _build_scope_app(
    middleware_cls: type,
    *,
    captured: dict[str, Any],
) -> FastAPI:
    """Build a FastAPI app with the given correlation middleware.

    The ``/echo`` route snapshots the live correlation context into the
    ``captured`` dict so tests can assert what the downstream handler
    observed without poking structlog internals.
    """
    app = FastAPI()
    app.add_middleware(middleware_cls)

    @app.get("/echo")
    async def echo() -> dict:
        captured["correlation_id"] = ctx.get_correlation_id()
        captured["request_id"] = ctx.get_request_id()
        return {"correlation_id": ctx.get_correlation_id()}

    return app


class TestPackageReexport:
    """``engine.middleware`` must re-export the raw-ASGI variant as the
    default ``CorrelationIdMiddleware``."""

    def test_default_correlation_id_middleware_is_raw_asgi(self):
        assert mw_pkg.CorrelationIdMiddleware is obs_middleware.CorrelationIdMiddleware, (
            "engine.middleware.CorrelationIdMiddleware must be the raw-ASGI variant"
        )

    def test_default_is_not_base_http_subclass(self):
        assert not issubclass(mw_pkg.CorrelationIdMiddleware, BaseHTTPMiddleware)

    def test_base_http_variant_is_reexported(self):
        assert mw_pkg.BaseHTTPCorrelationIdMiddleware is BaseHTTPCorrelationIdMiddleware
        assert issubclass(mw_pkg.BaseHTTPCorrelationIdMiddleware, BaseHTTPMiddleware)

    def test_all_exports(self):
        for name in (
            "CORRELATION_HEADER",
            "BaseHTTPCorrelationIdMiddleware",
            "CorrelationIdMiddleware",
        ):
            assert name in mw_pkg.__all__


class TestBaseHTTPClass:
    def test_class_is_base_http_subclass(self):
        assert issubclass(BaseHTTPCorrelationIdMiddleware, BaseHTTPMiddleware)

    def test_not_in_all_as_deprecated_alias(self):
        # The deprecated alias must not be advertised via __all__; only the
        # new name is a supported public symbol of correlation.py.
        assert "BaseHTTPCorrelationIdMiddleware" in corr_module.__all__
        assert "CorrelationIdMiddleware" not in corr_module.__all__


class TestDeprecatedAlias:
    """``CorrelationIdMiddleware`` in ``engine.middleware.correlation`` is a
    deprecated alias for ``BaseHTTPCorrelationIdMiddleware`` for one release
    cycle."""

    def test_alias_returns_base_http_class(self):
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            alias = corr_module.CorrelationIdMiddleware
        assert alias is BaseHTTPCorrelationIdMiddleware

    def test_alias_emits_deprecation_warning(self):
        with pytest.warns(DeprecationWarning, match=r"CorrelationIdMiddleware is deprecated"):
            _ = corr_module.CorrelationIdMiddleware

    def test_import_emits_deprecation_warning(self):
        import importlib

        # ``from engine.middleware.correlation import CorrelationIdMiddleware``
        # must trip the deprecation hook (PEP 562 __getattr__). Use the
        # cached module (not reload) so the returned class identity matches
        # the module's own BaseHTTPCorrelationIdMiddleware.
        module = importlib.import_module("engine.middleware.correlation")
        with pytest.warns(DeprecationWarning, match=r"CorrelationIdMiddleware is deprecated"):
            cls = module.CorrelationIdMiddleware
        assert cls is module.BaseHTTPCorrelationIdMiddleware

    def test_unknown_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            _ = corr_module.DefinitelyNotARealThing  # type: ignore[attr-defined]


class TestCreateAppRegistersCorrectClass:
    """The class-identity assertion in ``create_app`` must guarantee that the
    raw-ASGI variant is what gets registered."""

    @staticmethod
    def _correlation_entry(app):
        return next(
            (m for m in app.user_middleware if "Correlation" in m.cls.__name__),
            None,
        )

    def test_create_app_registers_raw_asgi_variant(self):
        app = create_app()
        entry = self._correlation_entry(app)
        assert entry is not None, "a correlation middleware must be registered"
        assert entry.cls is obs_middleware.CorrelationIdMiddleware
        assert entry.cls is not BaseHTTPCorrelationIdMiddleware

    def test_registered_class_is_not_base_http_subclass(self):
        app = create_app()
        entry = self._correlation_entry(app)
        assert not issubclass(entry.cls, BaseHTTPMiddleware)


@pytest.mark.parametrize("middleware_cls", _MIDDLEWARE_BUILDERS)
class TestStructlogContextBindingScopedToRequest:
    """For BOTH middleware builders the correlation context must be:

      * absent before the request,
      * bound (with the sanitized cid) while the request runs,
      * cleared again once the request completes.

    The two builders behave equivalently for ordinary (non-streaming,
    non-background) requests; the divergence on streaming / background
    work is covered separately for the raw-ASGI variant below.
    """

    async def test_structlog_context_binding_scoped_to_request(
        self, middleware_cls: type
    ):
        captured: dict[str, Any] = {}
        app = _build_scope_app(middleware_cls, captured=captured)

        # Precondition: nothing is bound in the outer (test) scope.
        _reset_structlog_context()
        assert ctx.get_correlation_id() is None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/echo", headers={"X-Correlation-Id": "scoped-cid-abc"})

        assert resp.status_code == 200
        # The downstream handler observed the sanitized cid.
        assert captured["correlation_id"] == "scoped-cid-abc"
        assert resp.json()["correlation_id"] == "scoped-cid-abc"
        # The sanitized cid is echoed back on the response header.
        assert resp.headers["X-Correlation-Id"] == "scoped-cid-abc"
        # Each request gets a distinct request_id.
        assert captured["request_id"] is not None
        # Post-request: the outer scope must NOT inherit the binding.
        assert ctx.get_correlation_id() is None

    async def test_context_does_not_leak_between_requests(self, middleware_cls: type):
        captured: dict[str, Any] = {}
        app = _build_scope_app(middleware_cls, captured=captured)

        _reset_structlog_context()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.get("/echo", headers={"X-Correlation-Id": "first-cid-aaa"})
            first_captured = captured.get("correlation_id")
            await ac.get("/echo", headers={"X-Correlation-Id": "second-cid-bbb"})
            second_captured = captured.get("correlation_id")

        assert first_captured == "first-cid-aaa"
        assert second_captured == "second-cid-bbb"
        # Final outer scope is clean.
        assert ctx.get_correlation_id() is None

    async def test_invalid_header_is_sanitized_not_leaked(self, middleware_cls: type):
        """The cid bound to the context must be the sanitized output of
        ``safe_correlation_id``, never the raw attacker-controlled header."""
        captured: dict[str, Any] = {}
        app = _build_scope_app(middleware_cls, captured=captured)

        _reset_structlog_context()
        transport = ASGITransport(app=app)
        # Malicious CRLF / Set-Cookie smuggling attempt.
        malicious = "legit\r\nSet-Cookie: pwn=1"
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/echo", headers={"X-Correlation-Id": malicious})

        assert resp.status_code == 200
        bound = captured["correlation_id"]
        # Bound value matches the response header (sanitized).
        assert bound == resp.headers["X-Correlation-Id"]
        # The raw malicious value did not survive sanitization.
        assert "\r" not in bound
        assert "\n" not in bound
        assert "Set-Cookie" not in bound
        # Sanitizer minted a fresh UUID4 in place of the bad value.
        uuid.UUID(bound)


class TestRawAsgiStreamingAndBackgroundScope:
    """The raw-ASGI variant keeps the correlation context bound for the
    full request lifecycle — including StreamingResponse body generation
    and BackgroundTask execution — and clears it afterwards.

    This is exactly the scenario where ``BaseHTTPMiddleware`` resets the
    context too early (see the warning in
    :mod:`engine.observability.middleware`), so these assertions only
    target the raw-ASGI variant.
    """

    @staticmethod
    def _build_streaming_app() -> tuple[FastAPI, dict[str, Any]]:
        captured: dict[str, Any] = {"chunks": []}
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/stream")
        async def stream() -> StreamingResponse:
            async def gen() -> asyncio.AsyncIterator[bytes]:
                # Body chunks are generated *after* dispatch returns in the
                # BaseHTTPMiddleware pattern; the raw-ASGI variant must
                # still have the context bound here.
                for _ in range(3):
                    captured["chunks"].append(ctx.get_correlation_id())
                    yield f"{ctx.get_correlation_id()}\n".encode()

            return StreamingResponse(gen(), media_type="text/plain")

        return app, captured

    @staticmethod
    def _build_background_app() -> tuple[FastAPI, dict[str, Any]]:
        from fastapi.responses import JSONResponse

        captured: dict[str, Any] = {"task": None}
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        def background_work() -> None:
            # BackgroundTasks run after the response is flushed but before
            # the inner ASGI app returns, so the raw-ASGI variant must
            # still have the context bound here.
            captured["task"] = ctx.get_correlation_id()

        @app.get("/bg")
        async def with_background() -> JSONResponse:
            return JSONResponse(
                {"ok": True}, background=BackgroundTask(background_work)
            )

        return app, captured

    async def test_streaming_response_context_is_scoped(self):
        app, captured = self._build_streaming_app()

        _reset_structlog_context()
        assert ctx.get_correlation_id() is None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/stream", headers={"X-Correlation-Id": "stream-cid-xyz"})

        assert resp.status_code == 200
        # Context was visible *during* body chunk generation (no early reset).
        assert captured["chunks"] == ["stream-cid-xyz"] * 3
        # Each line of the streamed body echoes the cid.
        assert resp.text.strip().split("\n") == ["stream-cid-xyz"] * 3
        # Response header carries the sanitized cid.
        assert resp.headers["X-Correlation-Id"] == "stream-cid-xyz"
        # Post-request: outer scope is clean (context did not leak).
        assert ctx.get_correlation_id() is None

    async def test_background_task_context_is_scoped(self):
        app, captured = self._build_background_app()

        _reset_structlog_context()
        assert ctx.get_correlation_id() is None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/bg", headers={"X-Correlation-Id": "bg-cid-123"})

        assert resp.status_code == 200
        # The background task observed the sanitized cid (no early reset).
        assert captured["task"] == "bg-cid-123"
        # Post-request: outer scope is clean (context did not leak past
        # the background task either).
        assert ctx.get_correlation_id() is None


class TestStateCorrelationIdBinding:
    """The raw-ASGI middleware normalises ``scope['state']`` to a real
    Starlette ``State`` and exposes the sanitized cid *and* the
    per-request ``request_id`` on it via attribute access, so downstream
    consumers can read them without going through contextvars. A plain
    ``dict`` left by an upstream raw-ASGI component is converted to a
    ``State`` while preserving its keys; an arbitrary object is coerced
    to a fresh ``State``; ``None`` yields a fresh ``State``.
    """

    @staticmethod
    async def _drive_with_state(
        initial_state: Any, *, cid_header: bytes = b"state-cid-999"
    ) -> tuple[Any, Any, Any]:
        """Drive the raw-ASGI middleware with a pre-populated
        ``scope['state']`` and return
        ``(state_after, downstream_saw_cid, downstream_saw_request_id)``."""
        seen: dict[str, Any] = {}

        async def downstream(scope, receive, send):
            seen["state"] = scope["state"]
            seen["cid"] = ctx.get_correlation_id()
            seen["request_id"] = ctx.get_request_id()
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        from engine.observability.middleware import _HEADER_NAME_BYTES

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(_HEADER_NAME_BYTES, cid_header)],
            "client": ("127.0.0.1", 60000),
            "server": ("test", 80),
            "state": initial_state,
        }

        mw = CorrelationIdMiddleware(downstream)
        await mw(scope, receive, send)
        return seen["state"], seen["cid"], seen["request_id"]

    async def test_state_attribute_set_when_state_is_starlette_state(self):
        state_after, cid, _request_id = await self._drive_with_state(State())
        # Attribute-style access works on a Starlette State.
        assert state_after.correlation_id == "state-cid-999"
        # Downstream observed the same sanitized cid in the context.
        assert cid == "state-cid-999"
        assert isinstance(state_after, State)

    async def test_state_dict_converted_to_state_preserving_keys(self):
        """A plain ``dict`` left by an upstream raw-ASGI component is
        converted to a Starlette ``State`` so attribute access works
        everywhere, while the dict's existing keys are preserved."""
        initial = {"upstream_key": "upstream_value"}
        state_after, cid, _request_id = await self._drive_with_state(initial)
        # The dict was converted to a State (not left as a dict, not
        # clobbered with an empty State).
        assert isinstance(state_after, State)
        # Attribute access works for both the freshly-set cid and the
        # pre-existing upstream key (preserved, not lost in conversion).
        assert state_after.correlation_id == "state-cid-999"
        assert state_after.upstream_key == "upstream_value"
        # Item access also works (Starlette State supports both styles),
        # so raw-ASGI consumers using dict subscript keep working.
        assert state_after["correlation_id"] == "state-cid-999"
        assert state_after["upstream_key"] == "upstream_value"
        assert set(iter(state_after)) >= {"correlation_id", "request_id", "upstream_key"}
        # Downstream observed the sanitized cid in the context.
        assert cid == "state-cid-999"

    async def test_state_arbitrary_object_coerced_to_fresh_state(self):
        """An arbitrary non-State, non-dict, non-None object cannot be
        trusted for attribute mutation, so it is coerced to a fresh
        ``State`` on which the cid and request_id are then set."""

        class ArbitraryBareObject:
            pass

        initial = ArbitraryBareObject()
        state_after, cid, _request_id = await self._drive_with_state(initial)
        # Coerced to a real Starlette State, not the original object.
        assert isinstance(state_after, State)
        assert state_after is not initial
        # The cid and request_id are visible on the fresh State.
        assert state_after.correlation_id == "state-cid-999"
        assert cid == "state-cid-999"

    async def test_state_initialised_when_missing(self):
        # When no upstream component populated state, the middleware
        # lazily initialises a Starlette State so attribute access works.
        state_after, _, _ = await self._drive_with_state(None)
        assert isinstance(state_after, State)
        assert state_after.correlation_id is not None

    async def test_request_id_visible_on_state(self):
        """``request_id`` must be set on ``scope['state']`` (attribute access)
        so downstream consumers can distinguish individual requests within
        a single correlation chain without touching contextvars. The
        value matches the one bound in the observability context."""
        state_after, _cid, request_id = await self._drive_with_state(State())
        # Both identifiers are present on the normalised State.
        assert state_after.correlation_id == "state-cid-999"
        assert isinstance(state_after, State)
        # request_id is a non-empty hex string.
        assert state_after.request_id == request_id
        assert isinstance(state_after.request_id, str)
        assert state_after.request_id
        # Distinct from the correlation id (request_id is per-request,
        # correlation id spans the whole causal chain).
        assert state_after.request_id != state_after.correlation_id
        # request_id is also visible after dict->State conversion.
        state_from_dict, _, rid_from_dict = await self._drive_with_state(
            {"upstream_key": "v"}
        )
        assert isinstance(state_from_dict, State)
        assert state_from_dict.request_id == rid_from_dict
        assert state_from_dict.request_id

    async def test_state_cid_is_sanitized_not_raw_header(self):
        """The value written to ``scope['state']`` must be the sanitized
        output of ``safe_correlation_id`` — never the raw attacker header."""
        seen: dict[str, Any] = {}

        async def downstream(scope, receive, send):
            seen["state"] = scope["state"]
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        from engine.observability.middleware import _HEADER_NAME_BYTES

        malicious = b"legit\r\nSet-Cookie: pwn=1"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(_HEADER_NAME_BYTES, malicious)],
            "client": ("127.0.0.1", 60000),
            "server": ("test", 80),
        }

        mw = CorrelationIdMiddleware(downstream)
        await mw(scope, receive, send)

        bound = seen["state"].correlation_id
        # The bound value must NOT be the raw attacker header.
        assert bound != malicious.decode("latin-1")
        # The raw attacker value did not survive sanitization.
        assert "\r" not in bound
        assert "\n" not in bound
        assert "Set-Cookie" not in bound
        # Sanitizer minted a fresh UUID4 in place of the bad value.
        uuid.UUID(bound)
        # The same sanitized value appears on the response header — both
        # channels (state attribute + response header) consume the same
        # sanitized ``cid`` rather than the raw header.
        resp_cid = None
        for message in sent:
            if message["type"] == "http.response.start":
                for name, value in message["headers"]:
                    if name == _HEADER_NAME_BYTES:
                        resp_cid = value.decode("latin-1")
        assert resp_cid is not None
        assert bound == resp_cid
