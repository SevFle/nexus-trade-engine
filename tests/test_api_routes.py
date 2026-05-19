"""Tests for API routes — backtest, marketplace, health."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

from engine.tasks.result_store import BacktestResultStore, set_result_store


@pytest.fixture(autouse=True)
def _setup_result_store():
    store = BacktestResultStore()
    set_result_store(store)


class TestHealthEndpoint:
    async def test_health_returns_ok(self, client: AsyncClient):
        response = await client.get("/health")
        assert response.status_code == HTTPStatus.OK
        assert response.json()["status"] == "ok"


class TestBacktestEndpoints:
    async def test_run_backtest_returns_accepted(self, client: AsyncClient):
        mock_task = AsyncMock()
        mock_task.kiq = AsyncMock()
        with patch("engine.tasks.worker.run_backtest_task", mock_task):
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
        assert data["backtest_id"] is not None

    async def test_get_result_unknown_id_returns_404(self, client: AsyncClient):
        response = await client.get("/api/v1/backtest/results/nonexistent-id")
        assert response.status_code == HTTPStatus.NOT_FOUND

    async def test_run_backtest_with_custom_capital(self, client: AsyncClient):
        mock_task = AsyncMock()
        mock_task.kiq = AsyncMock()
        with patch("engine.tasks.worker.run_backtest_task", mock_task):
            response = await client.post(
                "/api/v1/backtest/run",
                json={
                    "strategy_name": "mean_reversion_basic",
                    "symbol": "AAPL",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                    "initial_capital": 50000.0,
                },
            )
        assert response.status_code == HTTPStatus.OK

    async def test_run_backtest_invalid_body_returns_422(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/backtest/run",
            json={},
        )
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
