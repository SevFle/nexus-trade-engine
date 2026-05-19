"""Tests for async backtest execution via taskiq worker (issue #2).

Covers:
- BacktestResultStore local fallback operations
- API route dispatches via taskiq instead of BackgroundTasks
- Result endpoint reads from BacktestResultStore
- run_backtest_task persists results to the store
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.tasks.result_store import BacktestResultStore, set_result_store
from tests.conftest import FAKE_USER_ID


class TestBacktestResultStoreLocalFallback:
    async def test_set_running_and_get(self):
        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        await store.set_running(bt_id, "user-1", "my_strategy", "AAPL")
        result = await store.get(bt_id)
        assert result is not None
        assert result["status"] == "running"
        assert result["strategy_name"] == "my_strategy"
        assert result["symbol"] == "AAPL"

    async def test_set_completed_and_get(self):
        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        await store.set_completed(
            bt_id,
            "user-1",
            {
                "strategy_name": "mean_rev",
                "symbol": "MSFT",
                "initial_capital": 100_000.0,
                "final_value": 105_000.0,
                "metrics": {"sharpe_ratio": 1.2},
                "equity_curve": [],
                "trades": [],
            },
        )
        result = await store.get(bt_id)
        assert result is not None
        assert result["status"] == "completed"
        assert result["final_value"] == 105_000.0

    async def test_set_failed_and_get(self):
        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        await store.set_failed(
            bt_id, "user-1", "bad_strat", "AAPL", "Strategy not found", "ValueError"
        )
        result = await store.get(bt_id)
        assert result is not None
        assert result["status"] == "failed"
        assert result["error"] == "Strategy not found"
        assert result["error_type"] == "ValueError"

    async def test_get_nonexistent_returns_none(self):
        store = BacktestResultStore()
        result = await store.get("no-such-id")
        assert result is None

    async def test_delete_removes_entry(self):
        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        await store.set_running(bt_id, "user-1", "strat", "AAPL")
        await store.delete(bt_id)
        result = await store.get(bt_id)
        assert result is None

    async def test_evict_expired_removes_old_entries(self):
        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic() - 7200,
            "user-1",
            {"status": "running"},
        )
        await store.evict_expired()
        assert bt_id not in store._local_fallback

    async def test_overwrite_running_with_completed(self):
        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        await store.set_running(bt_id, "user-1", "strat", "AAPL")
        await store.set_completed(
            bt_id, "user-1", {"strategy_name": "strat", "symbol": "AAPL", "final_value": 100.0}
        )
        result = await store.get(bt_id)
        assert result["status"] == "completed"


class TestApiDispatchesViaTaskiq:
    async def test_run_backtest_dispatches_taskiq(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        set_result_store(store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/backtest")

        mock_task = AsyncMock()
        mock_task.kiq = AsyncMock()

        with patch("engine.tasks.worker.run_backtest_task", mock_task):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "mean_reversion_basic",
                        "symbol": "AAPL",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "accepted"
                assert data["backtest_id"] is not None

                mock_task.kiq.assert_awaited_once()
                call_kwargs = mock_task.kiq.call_args
                assert call_kwargs.kwargs["strategy_name"] == "mean_reversion_basic"
                assert call_kwargs.kwargs["symbol"] == "AAPL"

    async def test_run_sets_running_in_store(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        set_result_store(store)

        mock_task = AsyncMock()
        mock_task.kiq = AsyncMock()

        with patch("engine.tasks.worker.run_backtest_task", mock_task):
            app = FastAPI()
            app.include_router(router, prefix="/api/v1/backtest")

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "test_strat",
                        "symbol": "MSFT",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                    },
                )
                bt_id = resp.json()["backtest_id"]
                stored = await store.get(bt_id)
                assert stored is not None
                assert stored["status"] == "running"
                assert stored["strategy_name"] == "test_strat"


class TestResultEndpointReadsFromStore:
    async def test_result_not_found(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        set_result_store(store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/backtest")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/backtest/results/nonexistent-id")
            assert resp.status_code == 404
            assert resp.json()["status"] == "not_found"

    async def test_result_running_returns_202(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "running",
                "strategy_name": "test",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
            },
        )
        set_result_store(store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/backtest")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 202
            assert resp.json()["status"] == "running"

    async def test_result_completed_returns_data(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "completed",
                "strategy_name": "test_strat",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
                "initial_capital": 100_000.0,
                "final_value": 105_000.0,
                "metrics": {"sharpe_ratio": 1.5, "total_trades": 5},
                "equity_curve": [{"timestamp": "2024-01-01", "total_value": 100_000, "cash": 100_000}],
            },
        )
        set_result_store(store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/backtest")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["final_value"] == 105_000.0

    async def test_result_failed_returns_error(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {
                "status": "failed",
                "strategy_name": "test_strat",
                "symbol": "AAPL",
                "user_id": str(FAKE_USER_ID),
                "error": "Strategy not found",
                "error_type": "ValueError",
            },
        )
        set_result_store(store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/backtest")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "failed"
            assert "Strategy not found" in data["error"]

    async def test_result_forbidden_for_wrong_user(self):
        from engine.api.routes.backtest import router

        store = BacktestResultStore()
        bt_id = str(uuid.uuid4())
        store._local_fallback[bt_id] = (
            time.monotonic(),
            "different-user-id",
            {
                "status": "completed",
                "strategy_name": "test",
                "symbol": "AAPL",
                "user_id": "different-user-id",
            },
        )
        set_result_store(store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/backtest")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/backtest/results/{bt_id}")
            assert resp.status_code == 403


class TestRunBacktestTaskUsesStore:
    async def test_task_stores_completed_result(self):
        store = BacktestResultStore()
        set_result_store(store)
        bt_id = str(uuid.uuid4())

        mock_runner_result = MagicMock()
        mock_runner_result.trades = [{"side": "buy", "quantity": 10}]
        mock_runner_result.total_return_pct = 5.0
        mock_runner_result.final_capital = 105_000.0
        mock_runner_result.metrics = {"sharpe_ratio": 1.5}
        mock_runner_result.equity_curve = [{"ts": "2024-01-01", "total_value": 100_000}]

        mock_runner_instance = AsyncMock()
        mock_runner_instance.run.return_value = mock_runner_result
        mock_strategy = MagicMock()

        with (
            patch("engine.data.feeds.get_data_provider"),
            patch("engine.core.backtest_runner.BacktestConfig"),
            patch("engine.plugins.registry.PluginRegistry") as mock_registry_cls,
            patch("engine.core.backtest_runner.BacktestRunner", return_value=mock_runner_instance),
        ):
            mock_registry_cls.return_value.load_strategy.return_value = mock_strategy

            from engine.tasks.worker import run_backtest_task

            result = await run_backtest_task(
                backtest_id=bt_id,
                user_id="user-1",
                strategy_name="test_strat",
                symbol="AAPL",
                start_date="2024-01-01",
                end_date="2024-12-31",
                initial_capital=100_000.0,
            )

            assert result["status"] == "completed"
            assert result["backtest_id"] == bt_id

            stored = await store.get(bt_id)
            assert stored is not None
            assert stored["status"] == "completed"
            assert stored["final_value"] == 105_000.0

    async def test_task_stores_failed_result(self):
        store = BacktestResultStore()
        set_result_store(store)
        bt_id = str(uuid.uuid4())

        with patch("engine.data.feeds.get_data_provider", side_effect=RuntimeError("No data")):
            from engine.tasks.worker import run_backtest_task

            result = await run_backtest_task(
                backtest_id=bt_id,
                user_id="user-1",
                strategy_name="test_strat",
                symbol="AAPL",
                start_date="2024-01-01",
                end_date="2024-12-31",
                initial_capital=100_000.0,
            )

            assert result["status"] == "failed"
            assert "No data" in result["error"]

            stored = await store.get(bt_id)
            assert stored is not None
            assert stored["status"] == "failed"
            assert "No data" in stored["error"]
