"""Tests for the public legal disclaimer / risk-disclosure endpoints.

Covers:
* The ``LAST_UPDATED`` editorial stamp must never be a future date
  (``LAST_UPDATED <= date.today()``) so cache/version invariants hold on any
  host clock.
* The public disclaimer and risk-disclosure routes are served under the
  consistent ``/api/v1/legal/`` prefix.
* Public legal endpoints set a ``Cache-Control`` header for basic abuse
  mitigation.
* The category filter narrows the disclaimer list as documented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.legal import PUBLIC_LEGAL_CACHE_CONTROL
from engine.api.legal import router as legal_router
from engine.legal.disclaimers import (
    LAST_UPDATED,
    DisclaimerCategory,
    get_all_disclaimers,
)


def _build_app() -> FastAPI:
    """Build a minimal app mounting only the legal router.

    The disclaimer / risk-disclosure routes are public (no auth dependency),
    so no auth override is required. The autouse ``_bypass_auth`` conftest
    fixture is harmless here.
    """
    app = FastAPI()
    app.include_router(legal_router)
    return app


class TestLastUpdated:
    """Guard the editorial stamp invariant required by the spec."""

    def test_last_updated_not_in_the_future(self) -> None:
        # LAST_UPDATED must be a real, non-future date so that any client
        # relying on it for cache/version-gating behaves deterministically.
        assert datetime.now(tz=UTC).date() >= LAST_UPDATED

    def test_last_updated_is_a_date_instance(self) -> None:
        assert isinstance(LAST_UPDATED, date)


@pytest.mark.asyncio
class TestDisclaimerEndpoints:
    async def test_list_disclaimers_under_v1_prefix(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/disclaimers")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == len(body["disclaimers"])
        assert body["count"] == len(get_all_disclaimers())
        assert body["last_updated"] == LAST_UPDATED.isoformat()
        # All four canonical categories are represented when unfiltered.
        assert set(body["categories"]) == {c.value for c in DisclaimerCategory}

    async def test_disclaimers_old_prefix_is_gone(self) -> None:
        # The previously-inconsistent path (missing the /v1 segment) must now
        # 404 so callers cannot silently hit a stale route.
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/legal/disclaimers")
        assert resp.status_code == 404

    async def test_disclaimers_cache_control_header(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/disclaimers")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == PUBLIC_LEGAL_CACHE_CONTROL

    async def test_category_filter_narrows_results(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/legal/disclaimers",
                params={"category": DisclaimerCategory.WASH_SALE.value},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        assert all(
            d["category"] == DisclaimerCategory.WASH_SALE.value for d in body["disclaimers"]
        )
        assert body["categories"] == [DisclaimerCategory.WASH_SALE.value]

    async def test_unknown_category_rejected(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/legal/disclaimers", params={"category": "not-a-category"}
            )
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestRiskDisclosureEndpoint:
    async def test_risk_disclosures_under_v1_prefix(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/risk-disclosures")
        assert resp.status_code == 200
        body = resp.json()
        assert body["overview"]
        assert len(body["risk_factors"]) >= 1
        assert len(body["related_disclaimers"]) >= 1
        assert body["last_updated"] == LAST_UPDATED.isoformat()

    async def test_risk_disclosures_old_prefix_is_gone(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/legal/risk-disclosures")
        assert resp.status_code == 404

    async def test_risk_disclosures_cache_control_header(self) -> None:
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/risk-disclosures")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == PUBLIC_LEGAL_CACHE_CONTROL
