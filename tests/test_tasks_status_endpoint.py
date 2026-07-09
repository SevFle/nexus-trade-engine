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

* **The shared pool is never severed** — the PING fallback borrows the
  broker's ``connection_pool`` for a probe but must NEVER close/disconnect
  it, otherwise a liveness check would take the whole task subsystem down.
  The fallback binds a throwaway ``redis.asyncio.Redis`` client that is
  never closed (no ``async with`` / ``aclose()``) and bounds the PING in
  ``asyncio.wait_for(..., timeout=1.0)`` so a hung broker cannot wedge it.

A companion concern — that the lifespan actually invokes
``broker.startup()`` / ``broker.shutdown()`` — is covered by
``tests/test_task_broker.py`` at the broker-construction layer; this
test pins the public HTTP contract of the status endpoint.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(return_value=True)

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client):
        assert await _broker_is_running(broker) is True  # type: ignore[arg-type]
    fake_client.ping.assert_awaited_once()


async def test_broker_is_running_ping_failure_reports_not_running() -> None:
    """A failed PING never raises — it reports the broker as not running."""
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=ConnectionError("broker down"))

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client):
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
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=ConnectionError("broker down"))

    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    client = TestClient(app)

    with patch("redis.asyncio.Redis", return_value=fake_client):
        response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "stopped"
    assert body["broker_online"] is False


def test_status_endpoint_online_true_via_ping_fallback() -> None:
    """A successful PING fallback surfaces as ``broker_online: True`` end-to-end."""
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(return_value=True)

    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    client = TestClient(app)

    with patch("redis.asyncio.Redis", return_value=fake_client):
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


# ---------------------------------------------------------------------------
# The PING fallback must never close/sever the shared pool.
#
# The broker's ``connection_pool`` is owned by the app's task dispatch
# subsystem (opened/closed in the FastAPI lifespan). A liveness probe that
# borrows it for a PING must release *only* the borrowed connection and must
# NEVER call ``disconnect()``/``aclose()`` on the pool — otherwise a routine
# health check would take the entire task subsystem down. These tests pin
# that guarantee by asserting the pool's disconnect/close methods and the
# throwaway client's ``aclose()`` are never invoked.
# ---------------------------------------------------------------------------


async def test_broker_is_running_does_not_close_pool_after_ping() -> None:
    """The PING fallback must NEVER close/sever the broker's shared pool.

    The probe binds a throwaway ``redis.asyncio.Redis`` client to the
    broker's ``connection_pool`` but deliberately never closes it (no
    ``async with`` / ``aclose()``). After a *successful* PING we assert:
    the borrowed client's ``aclose()`` was never called, and the pool's
    ``disconnect()`` / ``aclose()`` were never called. This is the core
    regression guard against re-introducing an ``async with`` that would
    tear down the pool on every health check.
    """
    # A shared pool with spies on the close paths.
    pool = MagicMock(name="pool")
    pool.disconnect = AsyncMock(name="pool.disconnect")
    pool.aclose = AsyncMock(name="pool.aclose")

    # The throwaway client. It must expose ``aclose`` so we can prove it is
    # never awaited, and a working async ``ping``.
    fake_client = MagicMock(name="client")
    fake_client.ping = AsyncMock(return_value=True)
    fake_client.aclose = AsyncMock(name="client.aclose")

    broker = SimpleNamespace(connection_pool=pool)
    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await _broker_is_running(broker)

    assert result is True

    # Headline guarantee: the throwaway client is never closed…
    fake_client.aclose.assert_not_called()
    # …and neither is the shared pool.
    pool.disconnect.assert_not_called()
    pool.aclose.assert_not_called()


async def test_broker_is_running_does_not_close_pool_on_ping_failure() -> None:
    """Even when the PING fails, the shared pool is never closed.

    The error path (``except`` → ``return False``) must also avoid closing
    the pool: a failure during a health check must not cascade into taking
    task dispatch offline.
    """
    pool = MagicMock(name="pool")
    pool.disconnect = AsyncMock(name="pool.disconnect")
    pool.aclose = AsyncMock(name="pool.aclose")

    fake_client = MagicMock(name="client")
    fake_client.ping = AsyncMock(side_effect=ConnectionError("broker down"))
    fake_client.aclose = AsyncMock(name="client.aclose")

    broker = SimpleNamespace(connection_pool=pool)
    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await _broker_is_running(broker)

    assert result is False
    fake_client.aclose.assert_not_called()
    pool.disconnect.assert_not_called()
    pool.aclose.assert_not_called()


async def test_broker_is_running_does_not_use_context_manager() -> None:
    """The client must be a plain object, not driven via ``async with``.

    If the fallback used ``async with Redis(...)`` the previous bug (closing
    the client on context exit) would be re-introduced. We assert that the
    patched ``Redis`` is constructed with ``connection_pool=<pool>`` and
    that its async-context-manager dunders are never entered/exited.
    """
    pool = MagicMock(name="pool")

    fake_client = MagicMock(name="client")
    fake_client.ping = AsyncMock(return_value=True)
    fake_client.__aenter__ = AsyncMock(name="__aenter__")
    fake_client.__aexit__ = AsyncMock(name="__aexit__")

    redis_cls = MagicMock(return_value=fake_client)

    broker = SimpleNamespace(connection_pool=pool)
    with patch("redis.asyncio.Redis", redis_cls):
        result = await _broker_is_running(broker)

    assert result is True
    # The client is built exactly once, bound to the broker's shared pool.
    redis_cls.assert_called_once_with(connection_pool=pool)
    # And never driven as an async context manager.
    fake_client.__aenter__.assert_not_called()
    fake_client.__aexit__.assert_not_called()


def test_status_endpoint_does_not_close_pool_end_to_end() -> None:
    """End-to-end: a successful probe over HTTP leaves the shared pool open.

    Drives the real route through ``TestClient`` with a patched
    ``redis.asyncio.Redis`` whose pool spies on ``disconnect``/``aclose``.
    After a green probe neither the client nor the shared pool was closed,
    proving the contract holds through the full request path.
    """
    pool = MagicMock(name="pool")
    pool.disconnect = AsyncMock(name="pool.disconnect")
    pool.aclose = AsyncMock(name="pool.aclose")

    fake_client = MagicMock(name="client")
    fake_client.ping = AsyncMock(return_value=True)
    fake_client.aclose = AsyncMock(name="client.aclose")

    app = create_app()
    app.state.taskiq_broker = SimpleNamespace(connection_pool=pool)
    client = TestClient(app)

    with patch("redis.asyncio.Redis", return_value=fake_client):
        response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json()["broker_online"] is True

    fake_client.aclose.assert_not_called()
    pool.disconnect.assert_not_called()
    pool.aclose.assert_not_called()


# ---------------------------------------------------------------------------
# The PING must be bounded by ``asyncio.wait_for(..., timeout=1.0)`` so a
# hung broker can never wedge the probe, and the resulting timeout must be
# caught separately from connection errors.
# ---------------------------------------------------------------------------


def test_broker_is_running_source_uses_wait_for_with_one_second_timeout() -> None:
    """Static guard: the PING is wrapped in ``asyncio.wait_for(timeout=1.0)``.

    Pins the structural change so it is not silently regressed. Inspecting
    the source avoids depending on wall-clock timing in the test suite while
    still proving the bound exists.
    """
    source = inspect.getsource(_broker_is_running)
    assert "asyncio.wait_for(" in source
    assert "timeout=1.0" in source


async def test_broker_is_running_timeout_reports_not_running() -> None:
    """A ``PING`` that times out (broker hung) reports not running, not 500.

    ``asyncio.wait_for`` raises ``asyncio.TimeoutError`` when the bounded
    coroutine does not complete in time; that exception is caught separately
    from connection errors and treated as "not running". We simulate the
    timeout by having ``ping`` raise ``asyncio.TimeoutError`` directly
    (deterministic, no wall-clock wait).
    """
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=TimeoutError())

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await _broker_is_running(broker)

    assert result is False


async def test_broker_is_running_ping_cancelled_by_wait_for_when_hung() -> None:
    """A hung ``PING`` is cancelled by ``wait_for`` after the timeout.

    Proves the bound actually fires: a ``ping`` that blocks forever is
    cancelled when ``wait_for(timeout=1.0)`` elapses, the probe returns
    ``False``, and the hanging coroutine was cancelled rather than leaked.
    """
    cancelled = asyncio.Event()

    async def _hang_forever() -> bool:
        try:
            # Block well beyond the 1.0s bound — if wait_for is in place this
            # coroutine is cancelled and we never reach this point's end.
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return True

    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=_hang_forever)

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await _broker_is_running(broker)

    assert result is False
    # The hung coroutine was genuinely cancelled by the wait_for timeout.
    assert cancelled.is_set()


async def test_broker_is_running_timeout_does_not_close_pool() -> None:
    """A timeout must still leave the shared pool untouched."""
    pool = MagicMock(name="pool")
    pool.disconnect = AsyncMock(name="pool.disconnect")
    pool.aclose = AsyncMock(name="pool.aclose")

    fake_client = MagicMock(name="client")
    fake_client.ping = AsyncMock(side_effect=TimeoutError())
    fake_client.aclose = AsyncMock(name="client.aclose")

    broker = SimpleNamespace(connection_pool=pool)
    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await _broker_is_running(broker)

    assert result is False
    fake_client.aclose.assert_not_called()
    pool.disconnect.assert_not_called()
    pool.aclose.assert_not_called()


# ---------------------------------------------------------------------------
# The ``except`` is narrowed: only known infrastructure/connection errors are
# swallowed (→ "not running"). Programmer errors must propagate so they are
# surfaced rather than silently masked as a benign "broker down".
# ---------------------------------------------------------------------------


async def test_broker_is_running_narrowed_except_propagates_unexpected_error() -> None:
    """Unexpected exceptions are NOT swallowed — they propagate.

    The fallback only treats known connection/timeout/OS errors as "not
    running". A non-listed exception (e.g. ``ValueError`` from a buggy
    client) must surface instead of being silently mapped to ``False``,
    otherwise genuine bugs would hide behind a perpetual "broker stopped".
    """
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=ValueError("unexpected bug"))

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client), pytest.raises(ValueError):
        await _broker_is_running(broker)


async def test_broker_is_running_os_error_reports_not_running() -> None:
    """``OSError`` is in the narrowed catch set → not running."""
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=OSError("network unreachable"))

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client):
        assert await _broker_is_running(broker) is False


async def test_broker_is_running_builtin_timeout_reports_not_running() -> None:
    """The builtin ``TimeoutError`` is in the narrowed catch set → not running."""
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=TimeoutError("timed out"))

    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))
    with patch("redis.asyncio.Redis", return_value=fake_client):
        assert await _broker_is_running(broker) is False


async def test_broker_is_running_missing_redis_reports_not_running() -> None:
    """A missing ``redis`` dependency (``ImportError``) → not running.

    The lazy ``from redis.asyncio import Redis`` lives inside the try, so an
    absent dependency is reported as "not running" rather than crashing the
    probe with an ``ImportError``.
    """
    broker = SimpleNamespace(connection_pool=MagicMock(name="pool"))

    def _import_blow_up(*args, **kwargs):
        raise ImportError("no module named 'redis'")

    with patch("redis.asyncio.Redis", side_effect=_import_blow_up):
        result = await _broker_is_running(broker)

    assert result is False
