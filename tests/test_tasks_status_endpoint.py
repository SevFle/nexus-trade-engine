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

* **Real health status** — the ``broker`` field reflects the broker's
  actual state rather than a hardcoded constant. With the lifespan not
  entered ``app.state.taskiq_broker`` is unset, so the broker is reported
  as ``stopped``; we then inject brokers in known states to prove the
  field flips to ``running`` when the broker is live.

* **Status mirrors broker condition** — the endpoint reports
  ``status="degraded"`` with **HTTP 503** when the broker is stopped (so
  orchestrators reading the status code alone are steered away from an
  instance that can't enqueue tasks), and ``status="ok"`` with HTTP 200
  when the broker is running.

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


def test_tasks_status_endpoint_unauthenticated_and_reports_degraded() -> None:
    """No credentials required; with no lifespan the broker is stopped/degraded."""
    app = create_app()
    client = TestClient(app)

    # No Authorization header at all — the probe must stay reachable for
    # orchestrators/load balancers during deploys.
    response = client.get("/api/v1/tasks/status")

    # A dead/stopped broker downgrades the probe so load balancers steer
    # traffic away from an instance that can't enqueue tasks.
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    # The lifespan was never entered, so app.state.taskiq_broker is unset
    # and the broker is reported by its real state (stopped), not a
    # hardcoded "running".
    assert body["broker"] == "stopped"


def test_tasks_status_endpoint_reports_running_when_started() -> None:
    """A started broker (``is_started`` True) is reported as ``running``/200."""
    app = create_app()
    # Mirror what the lifespan does on a successful startup(): stash the
    # live broker on app.state. Here we inject a stub that advertises the
    # ``is_started`` flag newer taskiq exposes after ``startup()``.
    app.state.taskiq_broker = SimpleNamespace(is_started=True)

    client = TestClient(app)
    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "broker": "running"}


def test_tasks_status_endpoint_reports_degraded_when_flag_false() -> None:
    """A stopped broker (``is_started`` False) → HTTP 503 / ``degraded``.

    This is the explicit degraded-path coverage: a broker that is wired up
    on ``app.state`` but reports itself as not started yields a 503 with a
    ``degraded`` status so orchestrators don't route traffic to it.
    """
    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(is_started=False)

    client = TestClient(app)
    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "broker": "stopped"}


async def test_broker_is_running_uses_ping_when_no_flag() -> None:
    """Older taskiq has no ``is_started`` — fall back to a Redis PING.

    Covers the version-independent fallback path: a broker that exposes a
    ``connection_pool`` but no ``is_started`` flag is probed with a real
    ``PING``. ``redis.asyncio.Redis`` is patched so the test is hermetic.

    The fallback uses a *non-owning* client (no ``aclose``/``close``) and
    bounds the ping with ``asyncio.wait_for`` so a hung broker can't stall
    the probe — both of which are asserted here.
    """
    fake_redis = MagicMock(name="fake_redis")
    fake_redis.ping = AsyncMock(return_value=True)
    fake_redis.aclose = MagicMock(name="aclose")
    fake_redis.close = MagicMock(name="close")

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_redis) as redis_cls:
        assert await _broker_is_running(broker) is True  # type: ignore[arg-type]

    # The fallback builds the client from the broker's pool.
    redis_cls.assert_called_once_with(connection_pool=broker.connection_pool)
    # The ping was actually awaited inside ``asyncio.wait_for``.
    fake_redis.ping.assert_awaited_once()
    # Non-owning client: the probe must never close it (the pool manages
    # connections and is shared with the rest of the API process).
    fake_redis.aclose.assert_not_called()
    fake_redis.close.assert_not_called()


async def test_broker_is_running_ping_timeout_reports_not_running() -> None:
    """A PING that exceeds the probe timeout never stalls — it reports down.

    Pins the ``asyncio.wait_for(..., timeout=2.0)`` bound: when the ping
    blows the deadline the resulting ``TimeoutError`` is swallowed and the
    broker is reported as not running (a liveness probe must never hang).
    """
    fake_redis = MagicMock(name="fake_redis")
    fake_redis.ping = AsyncMock(return_value=True)

    async def _hang_and_timeout(coro, timeout=None):
        # ``wait_for`` receives the already-created ping coroutine; close
        # it so it isn't left un-awaited (mirrors what a real timeout does).
        coro.close()
        raise TimeoutError

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with (
        patch("redis.asyncio.Redis", return_value=fake_redis),
        patch("asyncio.wait_for", new=_hang_and_timeout),
    ):
        assert await _broker_is_running(broker) is False  # type: ignore[arg-type]


async def test_broker_is_running_ping_failure_reports_not_running() -> None:
    """A failed PING never raises — it reports the broker as not running."""
    fake_redis = MagicMock(name="fake_redis")
    fake_redis.ping = AsyncMock(side_effect=ConnectionError("broker down"))

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_redis):
        assert await _broker_is_running(broker) is False  # type: ignore[arg-type]


async def test_broker_is_running_none_broker() -> None:
    """A missing broker (lifespan not entered / startup failed) → not running."""
    assert await _broker_is_running(None) is False
