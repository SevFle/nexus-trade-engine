"""Tests for webhook dispatcher metrics emission (gh#80 follow-up).

The dispatcher emits the following metrics through the active
``MetricsBackend``:

- ``webhook.attempts`` — counter, one per HTTP attempt.
- ``webhook.duration_ms`` — histogram, observed once per attempt with
  the response ``status`` (or ``"network_error"``).
- ``webhook.delivered`` — counter, exactly once on terminal success.
- ``webhook.failed`` — counter, exactly once on terminal failure with
  ``reason ∈ {"non_retryable", "exhausted"}``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from engine.events.bus import EventBus
from engine.events.webhook_dispatcher import WebhookDispatcher
from engine.observability.metrics import RecordingBackend


class _FakeConfig:
    def __init__(
        self,
        *,
        template: str = "generic",
        max_retries: int = 3,
    ):
        self.id = "00000000-0000-0000-0000-000000000001"
        self.url = "https://example.com/hook"
        self.signing_secret = "topsecret"
        self.template = template
        self.max_retries = max_retries
        self.custom_headers: dict[str, str] = {}


class _FakeSession:
    def __init__(self) -> None:
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
def metrics() -> RecordingBackend:
    return RecordingBackend()


@pytest.fixture
def no_sleep():
    async def _zero(*_a, **_kw) -> None:
        return None

    return _zero


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _tags), v in backend.counters.items() if n == name)


def _counter_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


def _histogram_observations(
    backend: RecordingBackend, name: str
) -> list[tuple[tuple[tuple[str, str], ...], list[float]]]:
    return [(t, vs) for (n, t), vs in backend.histograms.items() if n == name]


class TestSuccessPath:
    async def test_delivered_emits_attempts_duration_and_delivered(
        self, session_factory, http_mock, metrics, no_sleep
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
            metrics=metrics,
        )

        await dispatcher.dispatch_one(session, _FakeConfig(), "test.event", {})

        assert _counter_total(metrics, "webhook.attempts") == 1
        assert _counter_total(metrics, "webhook.delivered") == 1
        assert _counter_total(metrics, "webhook.failed") == 0
        # Tags carry both event_type and template.
        assert (
            _counter_with(
                metrics,
                "webhook.delivered",
                {"event_type": "test.event", "template": "generic"},
            )
            == 1
        )
        # Histogram captured the 200 response.
        observations = _histogram_observations(metrics, "webhook.duration_ms")
        assert len(observations) == 1
        tags, values = observations[0]
        assert ("status", "200") in tags
        assert len(values) == 1


class TestNonRetryablePath:
    async def test_4xx_emits_failed_non_retryable(
        self, session_factory, http_mock, metrics, no_sleep
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
            metrics=metrics,
        )

        await dispatcher.dispatch_one(session, _FakeConfig(), "test.event", {})

        assert _counter_total(metrics, "webhook.attempts") == 1
        assert _counter_total(metrics, "webhook.delivered") == 0
        assert (
            _counter_with(
                metrics,
                "webhook.failed",
                {"event_type": "test.event", "reason": "non_retryable"},
            )
            == 1
        )


class TestExhaustedPath:
    async def test_5xx_until_exhausted_emits_attempts_per_try_and_failed_exhausted(
        self, session_factory, http_mock, metrics, no_sleep
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
            metrics=metrics,
        )

        await dispatcher.dispatch_one(
            session, _FakeConfig(max_retries=3), "test.event", {}
        )

        assert _counter_total(metrics, "webhook.attempts") == 3
        assert _counter_total(metrics, "webhook.delivered") == 0
        assert (
            _counter_with(
                metrics,
                "webhook.failed",
                {"event_type": "test.event", "reason": "exhausted"},
            )
            == 1
        )

    async def test_network_error_records_network_error_status(
        self, session_factory, http_mock, metrics, no_sleep
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
            metrics=metrics,
        )

        await dispatcher.dispatch_one(
            session, _FakeConfig(max_retries=3), "test.event", {}
        )

        # Two attempts: first network_error, second 200.
        assert _counter_total(metrics, "webhook.attempts") == 2
        assert _counter_total(metrics, "webhook.delivered") == 1
        statuses = {
            dict(tags).get("status")
            for tags, _ in _histogram_observations(metrics, "webhook.duration_ms")
        }
        assert statuses == {"network_error", "200"}


class TestDefaultBackend:
    async def test_resolves_get_metrics_when_not_injected(
        self, session_factory, http_mock, no_sleep
    ):
        # No metrics= injected → should fall back to the process singleton.
        from engine.observability.metrics import NullBackend, set_metrics

        recording = RecordingBackend()
        set_metrics(recording)
        try:
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
            await dispatcher.dispatch_one(
                session, _FakeConfig(), "test.event", {}
            )
            assert _counter_total(recording, "webhook.delivered") == 1
        finally:
            set_metrics(NullBackend())
