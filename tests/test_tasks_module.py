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

        with patch("engine.tasks.worker.ListQueueBroker", return_value=mock_broker_inst):
            with patch("engine.tasks.worker.RedisAsyncResultBackend"):
                with patch("engine.tasks.worker.CorrelationMiddleware"):
                    with patch("engine.tasks.worker.TaskiqScheduler"):
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
