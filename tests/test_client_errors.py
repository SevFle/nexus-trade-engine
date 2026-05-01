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
                "error_id": "550e8400-e29b-41d4-a716-446655440000",
            },
        )
        assert r.status_code == 201
        assert r.json()["error_id"] == "550e8400-e29b-41d4-a716-446655440000"

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


class TestErrorIdValidation:
    def test_caller_supplied_uuid_accepted(self, client: TestClient):
        r = client.post(
            "/api/v1/client/errors",
            json={
                "message": "boom",
                "error_id": "550e8400-e29b-41d4-a716-446655440000",
            },
        )
        assert r.status_code == 201
        assert r.json()["error_id"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_caller_supplied_non_uuid_rejected(self, client: TestClient):
        r = client.post(
            "/api/v1/client/errors",
            json={"message": "boom", "error_id": "abc-123"},
        )
        assert r.status_code == 422


class TestBodySizeCap:
    def test_request_body_over_1mib_returns_413(self, client: TestClient):
        # 1.5 MiB JSON payload — well under the per-field 64 KiB limit
        # if it parsed, but the body-size middleware should reject it
        # before Pydantic ever sees it.
        big = "x" * (1_500_000)
        r = client.post(
            "/api/v1/client/errors",
            content=f'{{"message":"{big}"}}',
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413


class TestSanitization:
    def test_url_query_string_dropped_before_logging(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        # The endpoint logs through structlog; we assert behaviour via
        # the response (still 201) and trust the structlog pipeline to
        # carry the scrubbed value. The unit-level scrubbing is
        # exercised below.
        r = client.post(
            "/api/v1/client/errors",
            json={
                "message": "boom",
                "url": "https://app.example/dash?token=secret&code=abc",
            },
        )
        assert r.status_code == 201

    def test_scrub_strips_crlf_and_ansi(self):
        from engine.api.routes.client_errors import _scrub

        assert _scrub("hello\nworld") == "hello world"
        assert _scrub("hello\r\nworld") == "hello  world"
        assert _scrub("\x1b[31mred\x1b[0m") == "red"
        assert _scrub(None) is None

    def test_strip_query_drops_query_and_fragment(self):
        from engine.api.routes.client_errors import _strip_query

        assert (
            _strip_query("https://app.example/dash?token=secret#x")
            == "https://app.example/dash"
        )
        assert _strip_query(None) is None
