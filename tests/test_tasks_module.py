"""Tests for engine.tasks package — validates module structure and imports."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


class TestTasksModule:
    @pytest.fixture(autouse=True)
    def _mock_redis_deps(self):
        mock_broker_inst = MagicMock()
        mock_broker_inst.with_result_backend.return_value = mock_broker_inst
        mock_broker_inst.with_middlewares.return_value = mock_broker_inst
        mock_broker_inst.task = lambda: (lambda f: f)

        mods_to_remove = [k for k in sys.modules if k.startswith("engine.tasks")]
        saved = {m: sys.modules.pop(m) for m in mods_to_remove}

        with (
            patch("engine.tasks.worker.ListQueueBroker", return_value=mock_broker_inst),
            patch("engine.tasks.worker.RedisAsyncResultBackend"),
            patch("engine.tasks.worker.CorrelationMiddleware"),
            patch("engine.tasks.worker.TaskiqScheduler"),
        ):
            yield

        for m in mods_to_remove:
            sys.modules.pop(m, None)
        sys.modules.update(saved)

    def test_worker_module_importable(self):
        from engine.tasks import worker

        assert hasattr(worker, "broker")
        assert hasattr(worker, "run_backtest_task")

    def test_tasks_package_importable(self):
        import engine.tasks

        assert engine.tasks is not None

    def test_worker_has_scheduler(self):
        from engine.tasks import worker

        assert hasattr(worker, "scheduler")

    def test_re_exports_accessible(self):
        from engine import tasks

        assert tasks.BacktestResultStore is not None
        assert tasks.broker is not None
        assert tasks.run_backtest_task is not None
        assert callable(tasks.get_result_store)
        assert callable(tasks.set_result_store)
