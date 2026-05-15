"""Comprehensive tests for engine.plugins.sandbox.executor — PluginSandboxExecutor."""

from __future__ import annotations

import asyncio

import pytest

from engine.core.signal import Side, Signal
from engine.plugins.sandbox.core.policy import ResourcePolicy, SandboxPolicy
from engine.plugins.sandbox.executor import PluginSandboxExecutor
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector


class _GoodStrategy:
    name = "good"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


class _AsyncGoodStrategy:
    name = "async_good"
    version = "1.0.0"

    async def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="TSLA", strategy_id=self.name)]


class _BadStrategy:
    name = "bad"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        raise RuntimeError("strategy crashed")


class _SlowStrategy:
    name = "slow"
    version = "1.0.0"

    async def on_bar(self, state, portfolio):
        await asyncio.sleep(60)
        return []


class _MixedSignalStrategy:
    name = "mixed"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [
            Signal.buy(symbol="AAPL", strategy_id=self.name),
            "not_a_signal",
            Signal.sell(symbol="MSFT", strategy_id=self.name),
            42,
        ]


class _EmptyStrategy:
    name = "empty"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class _NoIdSignalStrategy:
    name = "no_id_strat"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal(symbol="AAPL", side=Side.BUY, strategy_id="")]


@pytest.fixture
def policy() -> SandboxPolicy:
    return SandboxPolicy(
        plugin_id="test_plugin",
        resource_policy=ResourcePolicy(max_cpu_seconds=1),
    )


@pytest.fixture
def collector() -> SandboxMetricsCollector:
    return SandboxMetricsCollector()


class TestPluginSandboxExecutorInit:
    def test_stores_strategy_and_policy(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        assert executor.strategy.name == "good"
        assert executor.policy is policy

    def test_uses_provided_metrics_collector(self, policy: SandboxPolicy, collector: SandboxMetricsCollector) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        assert executor._metrics is collector


class TestPluginSandboxExecutorFromFactory:
    def test_from_factory_creates_executor(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor.from_factory(_GoodStrategy, policy)
        assert executor.strategy.name == "good"

    async def test_from_factory_produces_working_executor(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor.from_factory(_GoodStrategy, policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    def test_from_factory_blocks_dangerous_import(self, policy: SandboxPolicy) -> None:
        class _DangerousInit:
            name = "dangerous"
            version = "1.0.0"

            def __init__(self) -> None:
                import os  # noqa: F401

            def on_bar(self, s, p):
                return []

        with pytest.raises(ImportError, match="blocked"):
            PluginSandboxExecutor.from_factory(_DangerousInit, policy)


class TestSafeEvaluate:
    async def test_good_strategy(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    async def test_async_strategy(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_AsyncGoodStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "TSLA"
        finally:
            executor.cleanup()

    async def test_bad_strategy_returns_empty(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_BadStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_slow_strategy_times_out(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_SlowStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_empty_strategy_returns_empty(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()


class TestSignalConversion:
    async def test_filters_invalid_signals(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_MixedSignalStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 2
            symbols = {s.symbol for s in signals}
            assert symbols == {"AAPL", "MSFT"}
        finally:
            executor.cleanup()

    async def test_signal_id_injection(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_NoIdSignalStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].strategy_id == "no_id_strat"
        finally:
            executor.cleanup()


class TestMetricsRecording:
    async def test_records_successful_evaluation(
        self, policy: SandboxPolicy, collector: SandboxMetricsCollector
    ) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("test_plugin")
            assert metrics is not None
            assert metrics["total_evaluations"] == 1
            assert metrics["total_signals_emitted"] == 1
        finally:
            executor.cleanup()

    async def test_records_error(
        self, policy: SandboxPolicy, collector: SandboxMetricsCollector
    ) -> None:
        executor = PluginSandboxExecutor(_BadStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("test_plugin")
            assert metrics is not None
            assert metrics["errors"] == 1
            assert metrics["last_error"] is not None
        finally:
            executor.cleanup()

    async def test_records_timeout(
        self, policy: SandboxPolicy, collector: SandboxMetricsCollector
    ) -> None:
        executor = PluginSandboxExecutor(_SlowStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("test_plugin")
            assert metrics is not None
            assert metrics["errors"] == 1
            assert "Timeout" in (metrics["last_error"] or "")
        finally:
            executor.cleanup()


class TestGetHealth:
    async def test_health_report(
        self, policy: SandboxPolicy, collector: SandboxMetricsCollector
    ) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            health = executor.get_health()
            assert health["strategy_name"] == "good"
            assert health["version"] == "1.0.0"
            assert health["plugin_id"] == "test_plugin"
            assert health["trust_level"] == "untrusted"
            assert health["total_evaluations"] == 1
        finally:
            executor.cleanup()

    def test_health_before_evaluation(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        health = executor.get_health()
        assert health["strategy_name"] == "good"
        assert health["plugin_id"] == "test_plugin"


class TestCleanup:
    async def test_cleanup_safe(self, policy: SandboxPolicy) -> None:
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        await executor.safe_evaluate(None, None, None)
        executor.cleanup()
