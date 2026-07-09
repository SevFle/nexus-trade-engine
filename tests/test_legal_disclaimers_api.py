"""Targeted tests for the public legal disclaimers / risk-disclosure endpoints.

These tests pin down the fixes made to ``engine/api/legal.py`` and
``engine/legal/disclaimers.py``:

1. **Versioned route paths** — the public endpoints live under
   ``/api/v1/legal/...`` (not the legacy un-versioned ``/api/legal/...``).
2. **Fresh ``LAST_UPDATED``** — the editorial "last updated" date is today and
   propagates into both response payloads.
3. **Endpoint behaviour** — full disclaimer list, per-category filtering, 422
   on unknown categories, and the risk-disclosure payload shape.
4. **Public accessibility** — neither endpoint requires authentication.
5. **Per-IP rate limiting** — a shared token bucket caps both public endpoints;
   requests within the budget succeed, and an exhausted bucket yields a
   structured HTTP 429 with a ``Retry-After`` header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.legal import (
    get_public_legal_rate_bucket,
    rate_limit_public_legal_endpoint,
)
from engine.api.legal import (
    router as legal_router,
)
from engine.api.rate_limit import InMemoryBucketBackend, TokenBucket
from engine.legal.disclaimers import (
    LAST_UPDATED,
    build_disclaimer_list_response,
    get_risk_disclosure,
)

_DISCLAIMERS_PATH = "/api/v1/legal/disclaimers"
_RISK_PATH = "/api/v1/legal/risk-disclosures"
_LEGACY_DISCLAIMERS_PATH = "/api/legal/disclaimers"
_LEGACY_RISK_PATH = "/api/legal/risk-disclosures"


def _generous_bucket() -> TokenBucket:
    """A high-capacity, fast-refilling bucket so non-rate-limit tests are
    independent and never accidentally drain a shared module-level bucket."""
    return TokenBucket(
        backend=InMemoryBucketBackend(),
        capacity=1_000,
        refill_per_sec=1_000.0,
    )


def _draining_bucket(capacity: int = 1) -> TokenBucket:
    """A bucket that never refills, so the ``capacity``-th+1 request is blocked.

    ``refill_per_sec=0`` keeps behaviour deterministic — no real-time waits.
    """
    return TokenBucket(
        backend=InMemoryBucketBackend(),
        capacity=capacity,
        refill_per_sec=0.0,
    )


def _build_app(*, rate_bucket: TokenBucket) -> FastAPI:
    """Mount the legal router with the given (isolated) rate-limit bucket.

    Overriding :func:`get_public_legal_rate_bucket` keeps every test fully
    isolated — no reliance on the process-wide default bucket.
    """
    app = FastAPI()
    app.include_router(legal_router)
    app.dependency_overrides[get_public_legal_rate_bucket] = lambda: rate_bucket
    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A client whose public endpoints are effectively unthrottled."""
    app = _build_app(rate_bucket=_generous_bucket())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Fix 1: versioned route paths
# ---------------------------------------------------------------------------


class TestRoutePaths:
    def test_disclaimers_route_is_versioned(self) -> None:
        paths = {route.path for route in legal_router.routes}
        assert _DISCLAIMERS_PATH in paths
        # The legacy un-versioned path must NOT be registered.
        assert _LEGACY_DISCLAIMERS_PATH not in paths

    def test_risk_disclosures_route_is_versioned(self) -> None:
        paths = {route.path for route in legal_router.routes}
        assert _RISK_PATH in paths
        assert _LEGACY_RISK_PATH not in paths

    async def test_legacy_disclaimers_path_is_404(self, client: AsyncClient) -> None:
        # No matching route -> 404 before the rate-limit dependency runs.
        resp = await client.get(_LEGACY_DISCLAIMERS_PATH)
        assert resp.status_code == 404

    async def test_legacy_risk_disclosures_path_is_404(self, client: AsyncClient) -> None:
        resp = await client.get(_LEGACY_RISK_PATH)
        assert resp.status_code == 404

    def test_public_endpoints_carry_rate_limit_dependency(self) -> None:
        # Both public endpoints must declare the rate-limit dependency so a
        # misbehaving client cannot scrape them unthrottled.
        for target in (_DISCLAIMERS_PATH, _RISK_PATH):
            route = next(r for r in legal_router.routes if getattr(r, "path", None) == target)
            dep_callables = {
                getattr(dep, "dependency", dep) for dep in getattr(route, "dependencies", [])
            }
            assert rate_limit_public_legal_endpoint in dep_callables


# ---------------------------------------------------------------------------
# Fix 2: fresh LAST_UPDATED + propagation
# ---------------------------------------------------------------------------


class TestLastUpdated:
    def test_last_updated_is_today(self) -> None:
        # The editorial date is the single source of truth for client caching.
        # It must be bumped whenever content changes, so it must equal today.
        assert datetime.now(UTC).date() == LAST_UPDATED

    async def test_disclaimers_response_carries_last_updated(self, client: AsyncClient) -> None:
        resp = await client.get(_DISCLAIMERS_PATH)
        assert resp.status_code == 200
        assert resp.json()["last_updated"] == LAST_UPDATED.isoformat()

    async def test_risk_disclosure_response_carries_last_updated(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(_RISK_PATH)
        assert resp.status_code == 200
        assert resp.json()["last_updated"] == LAST_UPDATED.isoformat()


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------


class TestDisclaimersEndpoint:
    async def test_returns_all_disclaimers_without_filter(self, client: AsyncClient) -> None:
        resp = await client.get(_DISCLAIMERS_PATH)
        assert resp.status_code == 200
        body = resp.json()
        expected = build_disclaimer_list_response()
        assert body["count"] == expected.count
        assert body["count"] == len(body["disclaimers"])
        # count, list length, and categories field must agree.
        assert body["count"] > 0
        returned_cats = {d["category"] for d in body["disclaimers"]}
        assert returned_cats == {c.value for c in expected.categories}

    async def test_filter_by_category(self, client: AsyncClient) -> None:
        resp = await client.get(f"{_DISCLAIMERS_PATH}?category=trading_risk")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        for d in body["disclaimers"]:
            assert d["category"] == "trading_risk"
        # `categories` reflects only what is in the filtered result.
        assert body["categories"] == ["trading_risk"]

    async def test_unknown_category_rejected_with_422(self, client: AsyncClient) -> None:
        # FastAPI enum validation rejects values outside the StrEnum.
        resp = await client.get(f"{_DISCLAIMERS_PATH}?category=not-a-real-category")
        assert resp.status_code == 422

    async def test_each_disclaimer_has_required_fields(self, client: AsyncClient) -> None:
        resp = await client.get(_DISCLAIMERS_PATH)
        for d in resp.json()["disclaimers"]:
            assert d["id"]
            assert d["title"]
            assert d["summary"]
            assert d["category"] in {
                "trading_risk",
                "wash_sale",
                "tax_implications",
                "general",
            }
            assert d["severity"] in {"info", "warning", "critical"}

    async def test_response_matches_builder_exactly(self, client: AsyncClient) -> None:
        # The endpoint must be a thin wrapper around the pure builder so the
        # API and any non-HTTP caller (CLI/MCP) produce identical payloads.
        resp = await client.get(_DISCLAIMERS_PATH)
        expected = build_disclaimer_list_response().model_dump(mode="json")
        assert resp.json() == expected


class TestRiskDisclosuresEndpoint:
    async def test_returns_full_disclosure(self, client: AsyncClient) -> None:
        resp = await client.get(_RISK_PATH)
        assert resp.status_code == 200
        body = resp.json()
        expected = get_risk_disclosure()
        assert body["overview"] == expected.overview
        assert len(body["risk_factors"]) == len(expected.risk_factors)
        assert len(body["related_disclaimers"]) >= 1
        # related_disclaimers covers the loss-relevant categories only —
        # the "general" category is intentionally excluded.
        related_cats = {d["category"] for d in body["related_disclaimers"]}
        assert "general" not in related_cats

    async def test_each_risk_factor_well_formed(self, client: AsyncClient) -> None:
        resp = await client.get(_RISK_PATH)
        for factor in resp.json()["risk_factors"]:
            assert factor["id"]
            assert factor["title"]
            assert factor["description"]
            assert factor["severity"] in {"info", "warning", "critical"}

    async def test_response_matches_builder_exactly(self, client: AsyncClient) -> None:
        resp = await client.get(_RISK_PATH)
        expected = get_risk_disclosure().model_dump(mode="json")
        assert resp.json() == expected


class TestPublicAccessibility:
    """The disclaimers / risk-disclosure endpoints must work unauthenticated —
    they render on pre-login notice screens and during onboarding."""

    async def test_disclaimers_no_auth_header_required(self, client: AsyncClient) -> None:
        resp = await client.get(_DISCLAIMERS_PATH)
        assert resp.status_code == 200

    async def test_risk_disclosures_no_auth_header_required(self, client: AsyncClient) -> None:
        resp = await client.get(_RISK_PATH)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fix 3: per-IP rate limiting on the two public endpoints
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_allows_requests_within_budget(self) -> None:
        bucket = _draining_bucket(capacity=2)
        app = _build_app(rate_bucket=bucket)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            first = await ac.get(_DISCLAIMERS_PATH)
            second = await ac.get(_DISCLAIMERS_PATH)
        assert first.status_code == 200
        assert second.status_code == 200

    async def test_returns_429_when_budget_exhausted(self) -> None:
        bucket = _draining_bucket(capacity=1)
        app = _build_app(rate_bucket=bucket)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ok = await ac.get(_DISCLAIMERS_PATH)
            blocked = await ac.get(_DISCLAIMERS_PATH)
        assert ok.status_code == 200
        assert blocked.status_code == 429
        detail = blocked.json()["detail"]
        assert detail["code"] == "RATE_LIMIT_EXCEEDED"
        assert "retry_after" in detail
        assert int(detail["retry_after"]) >= 1
        # Retry-After header is present and agrees with the body.
        assert "retry-after" in {k.lower() for k in blocked.headers}
        assert int(blocked.headers["retry-after"]) == int(detail["retry_after"])

    async def test_risk_disclosures_endpoint_is_rate_limited(self) -> None:
        bucket = _draining_bucket(capacity=1)
        app = _build_app(rate_bucket=bucket)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ok = await ac.get(_RISK_PATH)
            blocked = await ac.get(_RISK_PATH)
        assert ok.status_code == 200
        assert blocked.status_code == 429

    async def test_shared_bucket_across_both_public_endpoints(self) -> None:
        # Both endpoints consume from the same bucket, so a client that burns
        # its budget on disclaimers is also blocked from risk-disclosures.
        bucket = _draining_bucket(capacity=1)
        app = _build_app(rate_bucket=bucket)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ok = await ac.get(_DISCLAIMERS_PATH)
            blocked = await ac.get(_RISK_PATH)
        assert ok.status_code == 200
        assert blocked.status_code == 429

    async def test_bucket_override_isolated_per_app(self) -> None:
        # Distinct apps get distinct buckets — exhausting one does not affect
        # another, which is what keeps tests independent.
        app_a = _build_app(rate_bucket=_draining_bucket(capacity=1))
        app_b = _build_app(rate_bucket=_generous_bucket())
        async with (
            AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as a,
            AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as b,
        ):
            # Drain app_a's bucket.
            await a.get(_DISCLAIMERS_PATH)
            assert (await a.get(_DISCLAIMERS_PATH)).status_code == 429
            # app_b is unaffected.
            assert (await b.get(_DISCLAIMERS_PATH)).status_code == 200
