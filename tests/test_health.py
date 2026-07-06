from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient


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
