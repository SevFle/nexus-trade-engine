"""Tests for the engine.middleware correlation middleware refactor.

Covers the rename of the ``BaseHTTPMiddleware``-based class to
``BaseHTTPCorrelationIdMiddleware`` (with a deprecated alias for one release
cycle), the ``engine.middleware`` package re-exporting the raw-ASGI variant
as the default ``CorrelationIdMiddleware``, and the class-identity assertion
in :func:`engine.app.create_app`.
"""

from __future__ import annotations

import warnings

import pytest
from starlette.middleware.base import BaseHTTPMiddleware

from engine import middleware as mw_pkg
from engine.app import create_app
from engine.middleware import correlation as corr_module
from engine.middleware.correlation import BaseHTTPCorrelationIdMiddleware
from engine.observability import middleware as obs_middleware


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


class TestCorrelationIdBehavior:
    """End-to-end behavior of the correlation middleware covering the four
    required contracts: (1) header passthrough, (2) auto-generation when the
    header is absent, (3) response-header presence, (4) structlog contextvar
    binding — plus the ``request.state.correlation_id`` contract that both
    middleware variants must satisfy.
    """

    HEADER = "X-Correlation-Id"

    @staticmethod
    def _probe_response(request):
        import structlog
        from starlette.responses import JSONResponse

        # Return what is bound to structlog contextvars *during* the request
        # plus the value surfaced on ``request.state`` — exercising both the
        # contextvar binding and the state contract.
        return JSONResponse(
            {
                "bound": structlog.contextvars.get_contextvars(),
                "state": getattr(request.state, "correlation_id", None),
            }
        )

    def _build_base_http_app(self):
        from starlette.applications import Starlette
        from starlette.routing import Route

        app = Starlette(routes=[Route("/probe", self._probe_response)])
        app.add_middleware(BaseHTTPCorrelationIdMiddleware)
        return app

    def _build_raw_asgi_app(self):
        from starlette.applications import Starlette
        from starlette.routing import Route

        from engine.observability.middleware import CorrelationIdMiddleware

        app = Starlette(routes=[Route("/probe", self._probe_response)])
        app.add_middleware(CorrelationIdMiddleware)
        return app

    def test_header_passthrough(self):
        from starlette.testclient import TestClient

        cid = "client-supplied-id-123"
        resp = TestClient(self._build_base_http_app()).get(
            "/probe", headers={self.HEADER: cid}
        )
        assert resp.status_code == 200
        assert resp.headers[self.HEADER] == cid                # echoed back
        assert resp.json()["bound"]["correlation_id"] == cid  # structlog bound
        assert resp.json()["state"] == cid                     # request.state set

    def test_auto_generated_when_absent(self):
        import uuid

        from starlette.testclient import TestClient

        resp = TestClient(self._build_base_http_app()).get("/probe")
        assert resp.status_code == 200
        cid = resp.headers[self.HEADER]
        assert uuid.UUID(cid).version == 4          # fresh uuid4 was minted
        assert resp.json()["bound"]["correlation_id"] == cid
        assert resp.json()["state"] == cid

    def test_response_header_always_present(self):
        from starlette.testclient import TestClient

        client = TestClient(self._build_base_http_app())
        # Absent incoming header -> generated and still echoed back.
        assert self.HEADER in client.get("/probe").headers
        # Provided incoming header -> echoed back unchanged.
        assert (
            client.get("/probe", headers={self.HEADER: "abc"}).headers[self.HEADER]
            == "abc"
        )

    def test_structlog_context_binding_scoped_to_request(self):
        import structlog
        from starlette.testclient import TestClient

        structlog.contextvars.clear_contextvars()
        resp = TestClient(self._build_base_http_app()).get(
            "/probe", headers={self.HEADER: "log-ctx-1"}
        )
        bound = resp.json()["bound"]
        assert bound["correlation_id"] == "log-ctx-1"   # bound during request
        assert "request_id" in bound                      # per-request id too
        # After the request completes the binding must be reset (no leak into
        # the next request that happens to share this task context).
        assert "correlation_id" not in structlog.contextvars.get_contextvars()

    @pytest.mark.parametrize("builder", ["base_http", "raw_asgi"])
    def test_request_state_correlation_id_contract(self, builder):
        """Both middleware variants must expose ``request.state.correlation_id``.
        The raw-ASGI variant is what ``create_app`` registers by default, so it
        must satisfy the same public contract as the BaseHTTP variant."""
        from starlette.testclient import TestClient

        app = (
            self._build_base_http_app()
            if builder == "base_http"
            else self._build_raw_asgi_app()
        )
        resp = TestClient(app).get("/probe", headers={self.HEADER: "state-xyz"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "state-xyz"
