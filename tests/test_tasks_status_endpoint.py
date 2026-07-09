"""Tests for ``GET /api/v1/tasks/status``.

Drives the full ``create_app()`` ASGI app via Starlette's ``TestClient``.

The route is an *infrastructure* liveness probe, so the contract pinned
here is:

* **Unauthenticated access works** — no credentials are required, so
  orchestrators/load balancers can reach it during deploys. The lifespan
  is intentionally *not* entered (no ``with`` block) so the test stays
  hermetic: it requires no live Valkey/Redis, event bus or DB connection,
  mirroring how the rest of the route-level suite drives ``create_app()``
  via a transport without booting the lifespan.

* **Always 200** — a broker outage must never flip the HTTP status (which
  would trip orchestrator restarts / load-balancer draining). Instead the
  endpoint answers 200 and exposes a machine-readable ``broker_online``
  boolean so callers branch on the body, not the status code.

* **Real health status** — the ``broker`` field reflects the broker's
  actual state rather than a hardcoded constant. With the lifespan not
  entered ``app.state.taskiq_broker`` is unset, so the broker is reported
  as ``stopped`` (and ``broker_online: False``); we then inject brokers
  in known states to prove the field flips to ``running`` / ``True`` when
  the broker is live.

A companion concern — that the lifespan actually invokes
``broker.startup()`` / ``broker.shutdown()`` — is covered by
``tests/test_task_broker.py`` at the broker-construction layer; this
test pins the public HTTP contract of the status endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from engine.api.routes.tasks import _broker_is_running
from engine.app import create_app


def test_tasks_status_endpoint_unauthenticated_and_reports_stopped() -> None:
    """No credentials required, and with no lifespan the broker is stopped.

    The probe must **always** answer 200 even when the broker is down so a
    broker outage never trips orchestrator restarts — callers inspect the
    ``broker_online`` boolean instead.
    """
    app = create_app()
    client = TestClient(app)

    # No Authorization header at all — the probe must stay reachable for
    # orchestrators/load balancers during deploys.
    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # The lifespan was never entered, so app.state.taskiq_broker is unset
    # and the broker is reported by its real state (stopped), not a
    # hardcoded "running".
    assert body["broker"] == "stopped"
    # Machine-readable mirror of the ``broker`` string.
    assert body["broker_online"] is False


def test_tasks_status_endpoint_reports_running_when_started() -> None:
    """A started broker (``is_started`` True) is reported as ``running``."""
    app = create_app()
    # Mirror what the lifespan does on a successful startup(): stash the
    # live broker on app.state. Here we inject a stub that advertises the
    # ``is_started`` flag newer taskiq exposes after ``startup()``.
    app.state.taskiq_broker = SimpleNamespace(is_started=True)

    client = TestClient(app)
    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "broker": "running",
        "broker_online": True,
    }


def test_tasks_status_endpoint_reports_stopped_when_flag_false() -> None:
    """A broker whose ``is_started`` is False is reported as ``stopped``.

    Even with the broker explicitly down the probe stays 200 — the
    ``broker_online: False`` field carries the bad news.
    """
    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(is_started=False)

    client = TestClient(app)
    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "broker": "stopped",
        "broker_online": False,
    }


async def test_broker_is_running_uses_ping_when_no_flag() -> None:
    """Older taskiq has no ``is_started`` — fall back to a Redis PING.

    Covers the version-independent fallback path: a broker that exposes a
    ``connection_pool`` but no ``is_started`` flag is probed with a real
    ``PING``. ``redis.asyncio.Redis`` is patched so the test is hermetic.
    """
    fake_redis = MagicMock()
    fake_redis.__aenter__ = AsyncMock(return_value=fake_redis)
    fake_redis.__aexit__ = AsyncMock(return_value=None)
    fake_redis.ping = AsyncMock(return_value=True)

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_redis):
        assert await _broker_is_running(broker) is True  # type: ignore[arg-type]
    fake_redis.ping.assert_awaited_once()


async def test_broker_is_running_ping_failure_reports_not_running() -> None:
    """A failed PING never raises — it reports the broker as not running."""
    fake_redis = MagicMock()
    fake_redis.__aenter__ = AsyncMock(return_value=fake_redis)
    fake_redis.__aexit__ = AsyncMock(return_value=None)
    fake_redis.ping = AsyncMock(side_effect=ConnectionError("broker down"))

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_redis):
        assert await _broker_is_running(broker) is False  # type: ignore[arg-type]


async def test_broker_is_running_none_broker() -> None:
    """A missing broker (lifespan not entered / startup failed) → not running."""
    assert await _broker_is_running(None) is False


def test_status_endpoint_always_200_with_broker_online_field() -> None:
    """The probe must always answer 200 and expose a ``broker_online`` bool.

    This is the headline contract change: a broker outage must not flip the
    HTTP status (which would cause orchestrators/load balancers to restart
    the API or drain it). Instead the endpoint stays 200 and reports the
    broker's health in the body via a real boolean ``broker_online``.
    """
    app = create_app()
    client = TestClient(app)

    # No lifespan → broker unset → broker considered stopped.
    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    body = response.json()
    # The field exists and is a genuine JSON boolean (not a string).
    assert "broker_online" in body
    assert body["broker_online"] is False
    assert isinstance(body["broker_online"], bool)


def test_status_endpoint_broker_online_is_true_for_running_broker() -> None:
    """``broker_online`` flips to True when the broker reports as started."""
    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(is_started=True)
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    body = response.json()
    assert body["broker_online"] is True
    assert isinstance(body["broker_online"], bool)


def test_status_endpoint_broker_string_and_bool_stay_consistent() -> None:
    """``broker`` (string) and ``broker_online`` (bool) must never disagree."""
    app = create_app()
    client = TestClient(app)

    # Stopped case.
    response_stopped = client.get("/api/v1/tasks/status")
    body_stopped = response_stopped.json()
    assert body_stopped["broker"] == "stopped"
    assert body_stopped["broker_online"] is False

    # Running case.
    app.state.taskiq_broker = SimpleNamespace(is_started=True)
    response_running = client.get("/api/v1/tasks/status")
    body_running = response_running.json()
    assert body_running["broker"] == "running"
    assert body_running["broker_online"] is True


def test_status_endpoint_returns_json_content_type() -> None:
    """The probe body is JSON so machine parsers can rely on Content-Type."""
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_status_endpoint_body_keys_are_stable() -> None:
    """Pin the exact set of response keys to catch accidental field drift."""
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert set(response.json().keys()) == {"status", "broker", "broker_online"}


def test_status_endpoint_500_when_broker_ping_raises_through_body() -> None:
    """``_broker_is_running`` swallows ping errors → still 200, online False.

    A live broker (``is_started`` absent) whose ``connection_pool`` exists
    but raises on ``PING`` must not take the endpoint down. The fallback
    catches the exception, reports ``broker_online: False``, and keeps the
    HTTP response green.
    """
    fake_redis = MagicMock()
    fake_redis.__aenter__ = AsyncMock(return_value=fake_redis)
    fake_redis.__aexit__ = AsyncMock(return_value=None)
    fake_redis.ping = AsyncMock(side_effect=ConnectionError("broker down"))

    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    client = TestClient(app)

    with patch("redis.asyncio.Redis", return_value=fake_redis):
        response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "stopped"
    assert body["broker_online"] is False


def test_status_endpoint_online_true_via_ping_fallback() -> None:
    """A successful PING fallback surfaces as ``broker_online: True`` end-to-end."""
    fake_redis = MagicMock()
    fake_redis.__aenter__ = AsyncMock(return_value=fake_redis)
    fake_redis.__aexit__ = AsyncMock(return_value=None)
    fake_redis.ping = AsyncMock(return_value=True)

    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    client = TestClient(app)

    with patch("redis.asyncio.Redis", return_value=fake_redis):
        response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "running"
    assert body["broker_online"] is True


async def test_broker_is_running_broker_without_pool_or_flag() -> None:
    """A broker with neither ``is_started`` nor ``connection_pool`` → stopped."""
    # A bare object that exposes neither signal cannot be probed.
    assert await _broker_is_running(SimpleNamespace()) is False


async def test_broker_is_running_is_started_truthy_non_bool() -> None:
    """The ``is_started`` flag is coerced via ``bool()`` (truthy → running)."""
    broker = SimpleNamespace(is_started=1)  # truthy, non-bool
    assert await _broker_is_running(broker) is True


async def test_broker_is_running_is_started_falsy_non_bool() -> None:
    """A falsy ``is_started`` value (e.g. ``0``) coerces to not running."""
    broker = SimpleNamespace(is_started=0)  # falsy, non-bool
    assert await _broker_is_running(broker) is False
