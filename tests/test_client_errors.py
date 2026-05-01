"""Tests for engine.api.routes.client_errors — client-side error reporting."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class TestErrorIngest:
    def test_post_accepts_minimal_payload(self, client: TestClient):
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "boom"},
        )
        assert r.status_code == 201
        body = r.json()
        assert "error_id" in body
        assert isinstance(body["error_id"], str)
        assert len(body["error_id"]) >= 8

    def test_post_accepts_full_payload(self, client: TestClient):
        r = client.post(
            "/api/v1/client/errors",
            json={
                "message": "TypeError: x is undefined",
                "stack": "at Foo (Foo.tsx:10)",
                "component_stack": "in Foo\n  in Bar",
                "url": "https://example.com/dashboard",
                "user_agent": "Mozilla/5.0",
                "error_id": "abc-123",
            },
        )
        assert r.status_code == 201
        assert r.json()["error_id"] == "abc-123"

    def test_post_rejects_empty_message(self, client: TestClient):
        r = client.post("/api/v1/client/errors", json={"message": ""})
        assert r.status_code == 422

    def test_post_rejects_missing_message(self, client: TestClient):
        r = client.post("/api/v1/client/errors", json={})
        assert r.status_code == 422

    def test_post_rejects_oversized_message(self, client: TestClient):
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "x" * (64 * 1024 + 1)},
        )
        assert r.status_code == 422

    def test_post_rejects_oversized_stack(self, client: TestClient):
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "boom", "stack": "x" * (64 * 1024 + 1)},
        )
        assert r.status_code == 422

    def test_get_not_allowed(self, client: TestClient):
        r = client.get("/api/v1/client/errors")
        assert r.status_code == 405


class TestRateLimit:
    def test_repeated_posts_eventually_429(self, client: TestClient):
        # Default cap is 30 req / 60s / IP; a 40-request burst from a
        # single test client should hit the limit at least once.
        seen_429 = False
        for _ in range(40):
            r = client.post(
                "/api/v1/client/errors", json={"message": "boom"}
            )
            if r.status_code == 429:
                seen_429 = True
                break
            assert r.status_code == 201
        assert seen_429, "rate limiter never tripped"

    def test_429_response_has_retry_after_header(self, client: TestClient):
        for _ in range(60):
            r = client.post(
                "/api/v1/client/errors", json={"message": "boom"}
            )
            if r.status_code == 429:
                assert "retry-after" in {k.lower() for k in r.headers}
                return
        pytest.fail("rate limiter never tripped within 60 requests")
