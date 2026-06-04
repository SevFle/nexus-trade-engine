"""Regression tests for the global ``require_legal_acceptance`` test bypass.

The conftest autouse fixture overrides ``require_legal_acceptance`` on every
FastAPI app so that tests don't need a ``legal_documents`` table. These tests
lock that contract in — if the override is ever removed, the protected routes
would 500 against a schema-less test DB and these tests will catch it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.router import api_router
from engine.legal.dependencies import require_legal_acceptance


async def _bypass_was_installed() -> bool:
    """Build a throwaway app and confirm the override is wired."""
    app = FastAPI()
    app.include_router(api_router)
    return require_legal_acceptance in app.dependency_overrides


@pytest.mark.asyncio
async def test_conftest_overrides_require_legal_acceptance() -> None:
    """The autouse conftest fixture must register an override on every new app."""
    assert await _bypass_was_installed(), (
        "require_legal_acceptance was not overridden — tests will hit the "
        "legal_documents table which doesn't exist in minimal test DBs."
    )


@pytest.mark.asyncio
async def test_protected_route_does_not_query_legal_documents() -> None:
    """Hit a route protected by require_legal_acceptance via the public client
    fixture and assert no legal_documents-related error surfaces.

    This is the regression that previously produced 62 failures (OperationalError:
    no such table: legal_documents) across test_health and test_auth_e2e.
    """
    # Build a fresh app the same way the `client` fixture does — the autouse
    # _bypass_auth / legal override must already be attached.
    import uuid as _uuid

    from engine.api.auth.dependency import get_current_user
    from engine.app import create_app
    from engine.db.models import User

    app = create_app()
    # Confirm override survived create_app's middleware/router wiring.
    assert require_legal_acceptance in app.dependency_overrides

    fake_user = User(
        id=_uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="regression@example.com",
        display_name="Regress",
        is_active=True,
        role="admin",
        auth_provider="local",
    )
    app.dependency_overrides[get_current_user] = lambda: fake_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # The marketplace router has require_legal_acceptance applied; without
        # the override this 500s against an empty SQLite DB.
        resp = await ac.get("/api/v1/marketplace/browse")
        # 200 or any non-5xx/non-451 response means legal_acceptance didn't fire.
        assert resp.status_code != 451, "require_legal_acceptance was not bypassed"
        assert resp.status_code < 500, (
            f"Protected route errored out — legal override likely missing: {resp.status_code} {resp.text}"
        )
