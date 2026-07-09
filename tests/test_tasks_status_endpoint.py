"""Tests for ``GET /api/v1/tasks/status``.

Covers the requirements the endpoint must satisfy:

* It is an *infrastructure* probe, so it MUST be reachable without an
  ``Authorization`` header (no ``Depends(get_current_user)``).
* It MUST accurately reflect the taskiq broker's readiness rather than
  returning a hardcoded ``{"broker": "running"}``.

The suite drives the full ``create_app()`` ASGI app via Starlette's
``TestClient`` **without** entering the lifespan, then drives the broker
readiness by manipulating ``app.state.taskiq_broker`` directly — mirroring
what :func:`engine.app._init_taskiq_broker` publishes during startup.
This keeps every test hermetic: no live Valkey/Redis, event bus or DB.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from engine.api.routes.tasks import _broker_ready
from engine.app import create_app

# ---------------------------------------------------------------------------
# Fake broker stand-ins
# ---------------------------------------------------------------------------


class _StatefulBroker:
    """Minimal stand-in matching the base ``AsyncBroker`` surface.

    The real ``ListQueueBroker`` exposes a ``state`` attribute (populated
    in ``AsyncBroker.__init__``) but no ``is_started`` / ``ping`` — this
    fake mirrors that surface so the readiness fallback path is exercised.
    """

    def __init__(self, *, state: object = object()) -> None:
        self.state = state


class _IsStartedBroker:
    """Broker that exposes an explicit ``is_started`` flag."""

    def __init__(self, *, is_started: bool) -> None:
        self.is_started = is_started


class _PingBroker:
    """Broker whose readiness is determined by an async ``ping()`` call."""

    def __init__(self, *, ping_value: object, raises: bool = False) -> None:
        self._ping_value = ping_value
        self._raises = raises
        self.ping_calls = 0

    async def ping(self) -> object:
        self.ping_calls += 1
        if self._raises:
            msg = "broker unreachable"
            raise RuntimeError(msg)
        return self._ping_value


# ---------------------------------------------------------------------------
# Unit tests for the readiness helper
# ---------------------------------------------------------------------------


def test_broker_ready_none_is_not_ready() -> None:
    import asyncio

    assert asyncio.run(_broker_ready(None)) is False


def test_broker_ready_stateful_broker_is_ready() -> None:
    import asyncio

    assert asyncio.run(_broker_ready(_StatefulBroker())) is True


def test_broker_ready_is_started_flag_wins() -> None:
    import asyncio

    assert asyncio.run(_broker_ready(_IsStartedBroker(is_started=True))) is True


def test_broker_ready_is_started_false_falls_through() -> None:
    """A broker that exposes ``is_started=False`` must not be flagged ready."""

    import asyncio

    # No ``state`` attribute on _IsStartedBroker → fallback also fails.
    assert asyncio.run(_broker_ready(_IsStartedBroker(is_started=False))) is False


def test_broker_ready_uses_ping_success() -> None:
    import asyncio

    broker = _PingBroker(ping_value=True)
    assert asyncio.run(_broker_ready(broker)) is True
    assert broker.ping_calls == 1


def test_broker_ready_ping_false_is_not_ready() -> None:
    import asyncio

    broker = _PingBroker(ping_value=False)
    assert asyncio.run(_broker_ready(broker)) is False
    assert broker.ping_calls == 1


def test_broker_ready_ping_truthy_non_bool_is_ready() -> None:
    import asyncio

    broker = _PingBroker(ping_value="pong")
    assert asyncio.run(_broker_ready(broker)) is True


def test_broker_ready_ping_exception_is_not_ready() -> None:
    """A ping() that raises must downgrade to not-ready, not propagate."""

    import asyncio

    broker = _PingBroker(ping_value=True, raises=True)
    assert asyncio.run(_broker_ready(broker)) is False
    assert broker.ping_calls == 1


# ---------------------------------------------------------------------------
# HTTP contract tests via the ASGI app
# ---------------------------------------------------------------------------


def test_endpoint_is_reachable_without_authorization_header() -> None:
    """Infrastructure probes carry no credentials; the route must allow that.

    The endpoint is hit with NO ``Authorization`` header at all. With a
    ready broker published into app state it must still return 200 —
    proving no ``Depends(get_current_user)`` is on the route.
    """
    app = create_app()
    app.state.taskiq_broker = _StatefulBroker()
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")  # no headers kwarg at all

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "broker": "ready"}


def test_endpoint_no_broker_returns_503_not_ready() -> None:
    """When the lifespan never wired a broker, the probe reports not-ready.

    This is the realistic "lifespan not entered / broker startup failed"
    path: ``app.state.taskiq_broker`` is absent (or None).
    """
    app = create_app()
    # Do NOT set app.state.taskiq_broker → getattr returns None.
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "broker": "not_ready"}


def test_endpoint_explicit_none_broker_returns_503() -> None:
    """A lifespan that recorded a failed startup sets the broker to None."""
    app = create_app()
    app.state.taskiq_broker = None
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "broker": "not_ready"}


def test_endpoint_reflects_stateful_broker_as_ready() -> None:
    app = create_app()
    app.state.taskiq_broker = _StatefulBroker()
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "broker": "ready"}


def test_endpoint_reflects_is_started_flag() -> None:
    app = create_app()
    app.state.taskiq_broker = _IsStartedBroker(is_started=True)
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "broker": "ready"}


def test_endpoint_reflects_ping_success() -> None:
    broker = _PingBroker(ping_value=True)
    app = create_app()
    app.state.taskiq_broker = broker
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "broker": "ready"}
    assert broker.ping_calls == 1


def test_endpoint_reflects_ping_failure_as_503() -> None:
    """A broker whose ping() raises is reported not-ready, not 500."""
    broker = _PingBroker(ping_value=True, raises=True)
    app = create_app()
    app.state.taskiq_broker = broker
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "broker": "not_ready"}
    assert broker.ping_calls == 1


def test_endpoint_broker_field_is_never_hardcoded_running() -> None:
    """Guard against regressing to the old hardcoded ``{"broker": "running"}``.

    Across both ready and not-ready states, the ``broker`` field must be a
    computed value ("ready"/"not_ready") — never the stale constant
    "running" that masked real outage state.
    """
    app = create_app()
    client = TestClient(app)

    # not-ready path
    app.state.taskiq_broker = None
    not_ready = client.get("/api/v1/tasks/status").json()
    assert not_ready["broker"] == "not_ready"

    # ready path
    app.state.taskiq_broker = _StatefulBroker()
    ready = client.get("/api/v1/tasks/status").json()
    assert ready["broker"] == "ready"

    assert "running" not in (not_ready["broker"], ready["broker"])
