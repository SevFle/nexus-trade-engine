"""Test-infrastructure guard for the ``require_legal_acceptance`` override.

The autouse ``_bypass_auth`` fixture in ``tests/conftest.py`` patches
``FastAPI.__init__`` so every app instance built during the test run has
``require_legal_acceptance`` short-circuited via ``dependency_overrides``.
This protects the suite against future production wiring (SEV-501 B2 /
ADR-0005) that would otherwise 451 every endpoint.

These tests are unit tests over the conftest plumbing itself — they do
not exercise the production dependency (that is the job of
``tests/test_legal_qa.py``).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import Depends, FastAPI

from engine.legal.dependencies import require_legal_acceptance


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app; the conftest monkey-patch is autouse."""
    app = FastAPI()

    @app.get("/probe")
    async def probe(_=Depends(require_legal_acceptance)):
        return {"ok": True}

    return app


class TestLegalAcceptanceOverride:
    def test_override_registered_on_isolated_app(self):
        app = _make_app()
        assert require_legal_acceptance in app.dependency_overrides

    def test_override_callable_returns_none(self):
        app = _make_app()
        stub = app.dependency_overrides[require_legal_acceptance]
        result = stub()
        if asyncio.iscoroutine(result):
            result = asyncio.get_event_loop().run_until_complete(result)
        assert result is None

    @pytest.mark.asyncio
    async def test_isolated_app_does_not_return_451(self):
        from httpx import ASGITransport, AsyncClient

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/probe")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_client_fixture_bypasses_legal_gate(self, client):
        """The shared ``client`` fixture must also carry the override."""
        # We don't assert a specific endpoint — only that the conftest's
        # client fixture has the override installed, regardless of which
        # routers the production app happens to mount.
        from engine.app import create_app as _prod_create_app  # noqa: F401

        # The fixture yields an httpx client whose underlying app is a
        # fresh ``create_app()`` instance; introspect the transport.
        transport_app = client._transport.app  # type: ignore[attr-defined]
        assert require_legal_acceptance in transport_app.dependency_overrides
