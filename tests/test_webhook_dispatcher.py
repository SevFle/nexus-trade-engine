"""Tests for the webhook dispatcher core (gh#80)."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from engine.events.bus import EventBus
from engine.events.webhook_dispatcher import (
    WebhookDispatcher,
    canonical_payload,
    render_template,
    sign_payload,
)


class TestSignPayload:
    def test_signature_format(self):
        sig = sign_payload("topsecret", b"hello")
        assert sig.startswith("sha256=")
        expected = hmac.new(b"topsecret", b"hello", hashlib.sha256).hexdigest()
        assert sig == f"sha256={expected}"

    def test_different_secrets_yield_different_signatures(self):
        a = sign_payload("aaa", b"x")
        b = sign_payload("bbb", b"x")
        assert a != b


class TestRenderTemplate:
    def _payload(self) -> dict[str, Any]:
        return canonical_payload("test.event", {"foo": "bar"})

    def test_generic_passthrough(self):
        p = self._payload()
        assert render_template("generic", p) == p

    def test_discord_shape(self):
        out = render_template("discord", self._payload())
        assert "embeds" in out
        assert out["embeds"][0]["title"] == "test.event"

    def test_slack_shape(self):
        out = render_template("slack", self._payload())
        assert out["blocks"][0]["type"] == "header"

    def test_telegram_shape(self):
        out = render_template("telegram", self._payload())
        assert out["parse_mode"] == "Markdown"
        assert "*test.event*" in out["text"]

    def test_unknown_template_falls_back_to_payload(self):
        p = self._payload()
        assert render_template("does-not-exist", p) == p


class _FakeConfig:
    def __init__(
        self,
        *,
        url: str = "https://example.com/hook",
        secret: str = "topsecret",  # noqa: S107
        template: str = "generic",
        max_retries: int = 3,
        custom_headers: dict | None = None,
    ):
        self.id = "00000000-0000-0000-0000-000000000001"
        self.url = url
        self.signing_secret = secret
        self.template = template
        self.max_retries = max_retries
        self.custom_headers = custom_headers or {}


class _FakeSession:
    def __init__(self):
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.fixture
def session_factory():
    session = _FakeSession()

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory, session


@pytest.fixture
def http_mock():
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    return client


@pytest.fixture
def no_sleep():
    async def _zero(*_a, **_kw) -> None:
        return None

    return _zero


class TestDispatchOne:
    async def test_2xx_marks_delivered_in_one_attempt(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "/")
        )
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig()
        delivery = await dispatcher.dispatch_one(session, cfg, "test.event", {"x": 1})
        assert delivery.status == "delivered"
        assert delivery.attempts == 1
        assert delivery.delivered_at is not None
        assert http_mock.post.await_count == 1

    async def test_4xx_marks_failed_no_retry(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.return_value = httpx.Response(
            404, request=httpx.Request("POST", "/")
        )
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig(max_retries=3)
        delivery = await dispatcher.dispatch_one(session, cfg, "test.event", {})
        assert delivery.status == "failed"
        assert delivery.attempts == 1
        assert "non-retryable" in delivery.error
        assert http_mock.post.await_count == 1

    async def test_5xx_retries_until_max_then_failed(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.return_value = httpx.Response(
            503, request=httpx.Request("POST", "/")
        )
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig(max_retries=3)
        delivery = await dispatcher.dispatch_one(session, cfg, "test.event", {})
        assert delivery.status == "failed"
        assert delivery.attempts == 3
        assert http_mock.post.await_count == 3

    async def test_5xx_then_2xx_recovers(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.side_effect = [
            httpx.Response(502, request=httpx.Request("POST", "/")),
            httpx.Response(200, request=httpx.Request("POST", "/")),
        ]
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig(max_retries=3)
        delivery = await dispatcher.dispatch_one(session, cfg, "test.event", {})
        assert delivery.status == "delivered"
        assert delivery.attempts == 2

    async def test_network_error_retried(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.side_effect = [
            httpx.ConnectError("boom"),
            httpx.Response(200, request=httpx.Request("POST", "/")),
        ]
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig(max_retries=3)
        delivery = await dispatcher.dispatch_one(session, cfg, "test.event", {})
        assert delivery.status == "delivered"
        assert delivery.attempts == 2

    async def test_signature_header_is_hmac_of_outgoing_body(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "/")
        )
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig(secret="topsecret")
        await dispatcher.dispatch_one(session, cfg, "test.event", {"k": "v"})
        call = http_mock.post.await_args
        body = call.kwargs["content"]
        headers = call.kwargs["headers"]
        expected = sign_payload("topsecret", body)
        assert headers["X-Nexus-Signature"] == expected
        assert headers["X-Nexus-Event"] == "test.event"

    async def test_custom_headers_merged(
        self, session_factory, http_mock, no_sleep
    ):
        factory, session = session_factory
        http_mock.post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "/")
        )
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=factory,
            http_client=http_mock,
            sleep_fn=no_sleep,
        )
        cfg = _FakeConfig(custom_headers={"X-Tenant": "acme"})
        await dispatcher.dispatch_one(session, cfg, "test.event", {})
        headers = http_mock.post.await_args.kwargs["headers"]
        assert headers["X-Tenant"] == "acme"


class TestCanonicalPayload:
    def test_shape(self):
        p = canonical_payload("foo", {"a": 1})
        assert p["event"] == "foo"
        assert p["data"] == {"a": 1}
        assert "timestamp" in p

    def test_payload_serializable(self):
        p = canonical_payload("foo", {"a": 1})
        s = json.dumps(p)
        assert "foo" in s
