"""Tests for engine.api.security_headers — security response headers."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.security_headers import (
    SecurityHeadersConfig,
    SecurityHeadersMiddleware,
    build_csp,
)


def _build_app(config: SecurityHeadersConfig | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SecurityHeadersMiddleware,
        config=config or SecurityHeadersConfig(),
    )

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    return app


@pytest.fixture
async def client():
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


class TestStaticHeaders:
    @pytest.mark.asyncio
    async def test_x_content_type_options_nosniff(self, client: AsyncClient):
        r = await client.get("/x")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_x_frame_options_deny(self, client: AsyncClient):
        r = await client.get("/x")
        assert r.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_referrer_policy(self, client: AsyncClient):
        r = await client.get("/x")
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    @pytest.mark.asyncio
    async def test_permissions_policy_locks_dangerous_apis(
        self, client: AsyncClient
    ):
        r = await client.get("/x")
        pp = r.headers.get("Permissions-Policy", "")
        for token in ("camera=()", "microphone=()", "geolocation=()"):
            assert token in pp


class TestHSTS:
    @pytest.mark.asyncio
    async def test_hsts_present_when_enabled_over_https(self):
        # Browsers ignore HSTS over HTTP; emit only when scheme is https
        # (or X-Forwarded-Proto: https when behind a TLS-terminating proxy).
        app = _build_app(SecurityHeadersConfig(hsts_enabled=True))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as ac:
            r = await ac.get("/x")
        v = r.headers.get("Strict-Transport-Security", "")
        assert "max-age=" in v
        assert "includeSubDomains" in v

    @pytest.mark.asyncio
    async def test_hsts_present_via_x_forwarded_proto(self):
        # TLS-terminating proxy forwards plain HTTP to the app but flags
        # the original scheme via X-Forwarded-Proto. HSTS must still emit.
        app = _build_app(SecurityHeadersConfig(hsts_enabled=True))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/x", headers={"X-Forwarded-Proto": "https"})
        assert "Strict-Transport-Security" in r.headers

    @pytest.mark.asyncio
    async def test_hsts_omitted_on_plain_http(self):
        app = _build_app(SecurityHeadersConfig(hsts_enabled=True))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/x")
        assert "Strict-Transport-Security" not in r.headers

    @pytest.mark.asyncio
    async def test_hsts_omitted_when_disabled(self):
        app = _build_app(SecurityHeadersConfig(hsts_enabled=False))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as ac:
            r = await ac.get("/x")
        assert "Strict-Transport-Security" not in r.headers


class TestCSP:
    @pytest.mark.asyncio
    async def test_csp_default_includes_self(self, client: AsyncClient):
        r = await client.get("/x")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp

    @pytest.mark.asyncio
    async def test_csp_blocks_object_src(self, client: AsyncClient):
        r = await client.get("/x")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "object-src 'none'" in csp

    @pytest.mark.asyncio
    async def test_csp_blocks_frame_ancestors(self, client: AsyncClient):
        r = await client.get("/x")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp

    @pytest.mark.asyncio
    async def test_csp_can_be_disabled(self):
        app = _build_app(SecurityHeadersConfig(csp_enabled=False))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/x")
        assert "Content-Security-Policy" not in r.headers


class TestCSPBuilder:
    def test_build_csp_basic(self):
        csp = build_csp(default_src=("'self'",), img_src=("'self'", "data:"))
        assert "default-src 'self'" in csp
        assert "img-src 'self' data:" in csp

    def test_build_csp_omits_unsafe_inline_for_script_by_default(self):
        csp = build_csp()
        script_segment = csp.split("script-src", 1)[-1].split(";", 1)[0]
        assert "'unsafe-inline'" not in script_segment

    def test_build_csp_includes_upgrade_insecure_requests(self):
        csp = build_csp()
        assert "upgrade-insecure-requests" in csp

    def test_build_csp_includes_report_uri_when_set(self):
        csp = build_csp(report_uri="/csp-report")
        assert "report-uri /csp-report" in csp

    def test_build_csp_rejects_unsafe_inline_in_script_src(self):
        with pytest.raises(ValueError, match="script_src"):
            build_csp(script_src=("'self'", "'unsafe-inline'"))

    def test_build_csp_rejects_unsafe_eval_in_script_src(self):
        with pytest.raises(ValueError, match="script_src"):
            build_csp(script_src=("'self'", "'unsafe-eval'"))

    def test_build_csp_rejects_wildcard_in_script_src(self):
        with pytest.raises(ValueError, match="script_src"):
            build_csp(script_src=("*",))

    def test_build_csp_rejects_wildcard_in_default_src(self):
        with pytest.raises(ValueError, match="default_src"):
            build_csp(default_src=("*",))


class TestPermissionsPolicy:
    @pytest.mark.asyncio
    async def test_browsing_topics_disabled(self, client: AsyncClient):
        # Topics API replaced FLoC; the new opt-out token must be present.
        r = await client.get("/x")
        assert "browsing-topics=()" in r.headers.get("Permissions-Policy", "")


class TestServerHeaderSuppression:
    @pytest.mark.asyncio
    async def test_server_header_blanked(self):
        # An upstream `Server: uvicorn` should not leak through.
        from fastapi import Response

        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, config=SecurityHeadersConfig())

        @app.get("/leaky")
        async def leaky() -> Response:
            r = Response('{"ok":true}', media_type="application/json")
            r.headers["Server"] = "uvicorn/0.99"
            return r

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/leaky")
        # Either absent or empty — never carrying the runtime fingerprint.
        assert resp.headers.get("Server", "") == ""


class TestExistingHeadersNotOverwritten:
    @pytest.mark.asyncio
    async def test_pre_set_csp_is_preserved(self):
        from fastapi import Response

        app = FastAPI()
        app.add_middleware(
            SecurityHeadersMiddleware, config=SecurityHeadersConfig()
        )

        @app.get("/y")
        async def y() -> Response:
            r = Response('{"ok":true}', media_type="application/json")
            r.headers["Content-Security-Policy"] = "custom"
            return r

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/y")
        assert r.headers.get("Content-Security-Policy") == "custom"


class TestNoLeakOnNon200:
    @pytest.mark.asyncio
    async def test_404_still_carries_security_headers(self):
        app = _build_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/missing")
        assert r.status_code == 404
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
