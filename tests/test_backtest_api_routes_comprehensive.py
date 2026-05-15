"""Integration tests for backtest API routes.

Covers additional edge cases beyond test_backtest_async_taskiq.py:
  - Run endpoint validation (missing fields, extra params)
  - Run endpoint with optional symbols/strategy_params/cost_config/interval
  - Result endpoint with rolling metrics in completed result
  - Result endpoint with drawdown_curve
  - Result endpoint with evaluation data
  - Multiple sequential runs produce different IDs
  - Evict expired entries before result retrieval
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.routes.backtest import router
from engine.tasks.result_store import BacktestResultStore, set_result_store
from tests.conftest import FAKE_USER_ID


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/backtest")
    return app


def _make_store() -> BacktestResultStore:
    store = BacktestResultStore()
    set_result_store(store)
    return store


def _mock_task() -> AsyncMock:
    mock = AsyncMock()
    mock.kiq = AsyncMock()
    return mock


# ─── Run endpoint edge cases ───────────────────────────────────────────


class TestRunEndpointValidation:
    async def test_run_with_all_optional_params(self) -> None:
        _make_store()
        mock = _mock_task()

        with patch("engine.tasks.worker.run_backtest_task", mock):
            app = _make_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "test_strat",
                        "symbol": "AAPL",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                        "initial_capital": 50_000.0,
                        "symbols": ["AAPL", "MSFT"],
                        "strategy_params": {"window": 20},
                        "cost_config": {"spread_bps": 5.0},
                        "interval": "1h",
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "accepted"

                call_kwargs = mock.kiq.call_args.kwargs
                assert call_kwargs["initial_capital"] == 50_000.0
                assert call_kwargs["symbols"] == ["AAPL", "MSFT"]
                assert call_kwargs["strategy_params"] == {"window": 20}
                assert call_kwargs["cost_config"] == {"spread_bps": 5.0}
                assert call_kwargs["interval"] == "1h"

    async def test_run_with_minimal_params(self) -> None:
        _make_store()
        mock = _mock_task()

        with patch("engine.tasks.worker.run_backtest_task", mock):
            app = _make_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "minimal",
                        "symbol": "GOOG",
                        "start_date": "2024-01-01",
                        "end_date": "2024-06-30",
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "accepted"
                assert data["backtest_id"] is not None

    async def test_run_returns_unique_backtest_ids(self) -> None:
        _make_store()
        mock = _mock_task()

        with patch("engine.tasks.worker.run_backtest_task", mock):
            app = _make_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp1 = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "strat1",
                        "symbol": "AAPL",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                    },
                )
                resp2 = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "strat2",
                        "symbol": "MSFT",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                    },
                )
                id1 = resp1.json()["backtest_id"]
                id2 = resp2.json()["backtest_id"]
                assert id1 != id2

    async def test_run_default_initial_capital(self) -> None:
        _make_store()
        mock = _mock_task()

        with patch("engine.tasks.worker.run_backtest_task", mock):
            app = _make_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "test",
                        "symbol": "AAPL",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                    },
                )
                call_kwargs = mock.kiq.call_args.kwargs
                assert call_kwargs["initial_capital"] == 100_000.0


# ─── Result endpoint with rich data ────────────────────────────────────


class TestResultEndpointRichData:
    async def test_completed_with_rolling_metrics(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "completed",
                "strategy_name": "rolling_test",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
                "initial_capital": 100_000.0,
                "final_value": 110_000.0,
                "metrics": {
                    "total_return_pct": 10.0,
                    "annualized_return_pct": 12.0,
                    "sharpe_ratio": 1.5,
                    "sortino_ratio": 2.0,
                    "max_drawdown_pct": 5.0,
                    "max_drawdown_duration_days": 10,
                    "max_drawdown_recovery_days": 15,
                    "calmar_ratio": 2.4,
                    "volatility_annual_pct": 15.0,
                    "total_trades": 20,
                    "win_rate": 0.6,
                    "profit_factor": 1.8,
                    "avg_trade_pnl": 500.0,
                    "avg_winner": 800.0,
                    "avg_loser": -300.0,
                    "best_trade": 2000.0,
                    "worst_trade": -1500.0,
                    "max_consecutive_wins": 5,
                    "max_consecutive_losses": 3,
                    "total_costs": 200.0,
                    "total_taxes": 150.0,
                    "cost_drag_pct": 0.2,
                    "turnover_ratio": 1.5,
                    "exposure_pct": 80.0,
                    "rolling_metrics": [
                        {"window_days": 30, "sharpe_ratio": 1.2, "sortino_ratio": 1.5, "volatility_annual_pct": 14.0, "max_drawdown_pct": 3.0},
                    ],
                },
                "equity_curve": [
                    {"timestamp": "2024-01-01", "total_value": 100_000, "cash": 100_000},
                ],
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["final_value"] == 110_000.0
            assert len(data["metrics"]["rolling_metrics"]) == 1
            assert data["metrics"]["rolling_metrics"][0]["window_days"] == 30

    async def test_completed_with_evaluation(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "completed",
                "strategy_name": "eval_test",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
                "initial_capital": 100_000.0,
                "final_value": 105_000.0,
                "metrics": {
                    "total_return_pct": 5.0,
                    "annualized_return_pct": 6.0,
                    "sharpe_ratio": 1.0,
                    "sortino_ratio": 1.5,
                    "max_drawdown_pct": 3.0,
                    "max_drawdown_duration_days": 5,
                    "max_drawdown_recovery_days": 3,
                    "calmar_ratio": 2.0,
                    "volatility_annual_pct": 12.0,
                    "total_trades": 10,
                    "win_rate": 0.5,
                    "profit_factor": 1.5,
                    "avg_trade_pnl": 500.0,
                    "avg_winner": 800.0,
                    "avg_loser": -300.0,
                    "best_trade": 1500.0,
                    "worst_trade": -1000.0,
                    "max_consecutive_wins": 3,
                    "max_consecutive_losses": 2,
                    "total_costs": 100.0,
                    "total_taxes": 80.0,
                    "cost_drag_pct": 0.1,
                    "turnover_ratio": 1.0,
                    "exposure_pct": 70.0,
                    "evaluation": {
                        "composite_score": 75.0,
                        "grade": "B+",
                    },
                },
                "equity_curve": [],
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["evaluation"]["composite_score"] == 75.0
            assert data["evaluation"]["grade"] == "B+"

    async def test_completed_with_drawdown_curve(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "completed",
                "strategy_name": "dd_test",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
                "initial_capital": 100_000.0,
                "final_value": 98_000.0,
                "metrics": {
                    "total_return_pct": -2.0,
                    "annualized_return_pct": -2.5,
                    "sharpe_ratio": -0.5,
                    "sortino_ratio": -0.3,
                    "max_drawdown_pct": 5.0,
                    "max_drawdown_duration_days": 8,
                    "max_drawdown_recovery_days": 4,
                    "calmar_ratio": -0.5,
                    "volatility_annual_pct": 20.0,
                    "total_trades": 5,
                    "win_rate": 0.2,
                    "profit_factor": 0.5,
                    "avg_trade_pnl": -400.0,
                    "avg_winner": 600.0,
                    "avg_loser": -700.0,
                    "best_trade": 600.0,
                    "worst_trade": -1000.0,
                    "max_consecutive_wins": 1,
                    "max_consecutive_losses": 3,
                    "total_costs": 50.0,
                    "total_taxes": 30.0,
                    "cost_drag_pct": 0.05,
                    "turnover_ratio": 0.8,
                    "exposure_pct": 60.0,
                    "drawdown_curve": [0.0, -1.0, -2.5, -5.0, -3.0, -2.0],
                },
                "equity_curve": [],
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["drawdown_curve"] == [0.0, -1.0, -2.5, -5.0, -3.0, -2.0]

    async def test_result_with_partial_metrics(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "completed",
                "strategy_name": "partial",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
                "initial_capital": 100_000.0,
                "final_value": 102_000.0,
                "metrics": {
                    "sharpe_ratio": 0.8,
                    "total_trades": 3,
                },
                "equity_curve": [],
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["metrics"]["total_trades"] == 3
            assert data["metrics"]["total_return_pct"] == 0.0


# ─── Result endpoint evict expired ─────────────────────────────────────


class TestResultEndpointEviction:
    async def test_expired_entry_not_returned(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic() - 7200,
            str(FAKE_USER_ID),
            {
                "status": "completed",
                "strategy_name": "old",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 404
            assert resp.json()["status"] == "not_found"

    async def test_fresh_entry_returned(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "running",
                "strategy_name": "fresh",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 202


# ─── Backtest request model validation ─────────────────────────────────


class TestBacktestRequestModel:
    async def test_missing_strategy_name_returns_422(self) -> None:
        _make_store()
        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/backtest/run",
                json={
                    "symbol": "AAPL",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                },
            )
            assert resp.status_code == 422

    async def test_missing_symbol_returns_422(self) -> None:
        _make_store()
        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/backtest/run",
                json={
                    "strategy_name": "test",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                },
            )
            assert resp.status_code == 422

    async def test_missing_dates_returns_422(self) -> None:
        _make_store()
        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/backtest/run",
                json={
                    "strategy_name": "test",
                    "symbol": "AAPL",
                },
            )
            assert resp.status_code == 422

    async def test_result_with_no_user_id_defaults_empty(self) -> None:
        store = _make_store()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            "different-user",
            {
                "status": "completed",
                "strategy_name": "no_uid",
                "symbol": "AAPL",
                "user_id": "different-user",
            },
        )

        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 403
