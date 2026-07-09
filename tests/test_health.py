from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from engine.app import create_app


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_v1_alias_returns_ok(client: AsyncClient) -> None:
    # The k6 smoke load test hits GET /api/v1/health; keep it resolvable.
    response = await client.get("/api/v1/health")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_backtest_run_stub_returns_accepted(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/backtest/run",
        json={
            "strategy_name": "mean_reversion_basic",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
    )
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_backtest_root_submit_returns_202_with_load_test_payload(
    client: AsyncClient,
) -> None:
    # The k6 baseline load test POSTs to /api/v1/backtest (no /run)
    # with the strategy_id/start/end contract and expects 202/201/200.
    response = await client.post(
        "/api/v1/backtest",
        json={
            "strategy_id": "noop",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-02T00:00:00Z",
            "symbol": "AAPL",
        },
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    data = response.json()
    assert data["status"] == "accepted"
    assert data["backtest_id"]


@pytest.mark.asyncio
async def test_backtest_root_submit_accepts_canonical_fields(
    client: AsyncClient,
) -> None:
    # The root endpoint must also accept the canonical naming so existing
    # callers can use either spelling.
    response = await client.post(
        "/api/v1/backtest",
        json={
            "strategy_name": "noop",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
        },
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    assert response.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# /healthz — liveness probe (always 200, no dependency checks, no auth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_stays_up_without_dependencies(client: AsyncClient) -> None:
    # Liveness must NEVER depend on DB/Redis/broker being available. The
    # ``client`` fixture never enters the app lifespan, so none of those
    # dependencies are wired — yet /healthz still answers 200.
    response = await client.get("/healthz")
    assert response.status_code == HTTPStatus.OK
    # And the body carries no dependency status (it performs no checks).
    assert response.json() == {"status": "ok"}


def _healthy_db_factory() -> MagicMock:
    """A ``get_session_factory`` stand-in whose ``SELECT 1`` always succeeds."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_readyz_returns_200_when_all_dependencies_healthy() -> None:
    app = create_app()
    app.state.valkey = MagicMock(ping=AsyncMock(return_value=True))
    app.state.taskiq_broker = SimpleNamespace(is_started=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch(
            "engine.api.routes.health.get_session_factory",
            return_value=_healthy_db_factory(),
        ):
            response = await ac.get("/readyz")

    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["valkey"] == "ok"
    assert data["broker"] == "ok"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_redis_is_down() -> None:
    app = create_app()
    # DB and broker are healthy, but Redis is unreachable — readiness
    # must drop to 503 so the load balancer stops sending traffic.
    app.state.valkey = MagicMock(
        ping=AsyncMock(side_effect=ConnectionError("redis down"))
    )
    app.state.taskiq_broker = SimpleNamespace(is_started=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch(
            "engine.api.routes.health.get_session_factory",
            return_value=_healthy_db_factory(),
        ):
            response = await ac.get("/readyz")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "degraded"
    assert data["valkey"] == "error"
    # The other dependencies are fine — only Redis pulled readiness down.
    assert data["db"] == "ok"
    assert data["broker"] == "ok"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_broker_stopped() -> None:
    app = create_app()
    app.state.valkey = MagicMock(ping=AsyncMock(return_value=True))
    # A stopped broker (startup failed / never ran) makes the pod not ready.
    app.state.taskiq_broker = SimpleNamespace(is_started=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch(
            "engine.api.routes.health.get_session_factory",
            return_value=_healthy_db_factory(),
        ):
            response = await ac.get("/readyz")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "degraded"
    assert data["broker"] == "stopped"
    assert data["db"] == "ok"
    assert data["valkey"] == "ok"


@pytest.mark.asyncio
async def test_readyz_reports_db_error_without_500() -> None:
    # A down DB must surface as a structured 503, not an unhandled 500,
    # so the readiness probe stays queryable during a DB outage.
    app = create_app()
    app.state.valkey = MagicMock(ping=AsyncMock(return_value=True))
    app.state.taskiq_broker = SimpleNamespace(is_started=True)

    broken_factory = MagicMock()
    broken_factory.side_effect = OperationalError("db down", {}, None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch(
            "engine.api.routes.health.get_session_factory",
            return_value=broken_factory,
        ):
            response = await ac.get("/readyz")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "degraded"
    assert data["db"] == "error"
