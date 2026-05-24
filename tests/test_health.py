from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

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
async def test_backtest_run_stub_returns_accepted(client: AsyncClient) -> None:
    mock_store = AsyncMock()
    mock_store.set_running = AsyncMock()

    with (
        patch("engine.tasks.worker.run_backtest_task") as mock_task,
        patch("engine.tasks.result_store.get_result_store", return_value=mock_store),
    ):
        mock_task.kiq = AsyncMock()
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
