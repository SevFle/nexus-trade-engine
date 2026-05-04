from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestMainModule:
    def test_app_exists(self):
        import engine.main

        assert engine.main.app is not None
        assert engine.main.app.title == "nexus-trade-engine"

    def test_health_route_registered(self):
        import engine.main

        routes = [r.path for r in engine.main.app.routes]
        assert "/health" in routes

    async def test_health_check(self):
        import engine.main

        from engine.main import health_check

        result = await health_check()
        assert result["status"] == "healthy"
        assert result["version"] == "0.1.0"

    def test_routes_included(self):
        import engine.main

        routes = [r.path for r in engine.main.app.routes]
        assert any("/api/v1/portfolio" in r for r in routes)
        assert any("/api/v1/strategies" in r for r in routes)
        assert any("/api/v1/backtest" in r for r in routes)
        assert any("/api/v1/marketplace" in r for r in routes)


class TestTasksModule:
    def test_broker_importable(self):
        from engine.tasks import broker

        assert broker is not None

    def test_run_backtest_task_importable(self):
        from engine.tasks import run_backtest_task

        assert callable(run_backtest_task)

    def test_worker_broker_type(self):
        from engine.tasks.worker import broker

        from taskiq_redis.redis_broker import ListQueueBroker

        assert isinstance(broker, ListQueueBroker)

    def test_scheduler_importable(self):
        from engine.tasks.worker import scheduler

        assert scheduler is not None
