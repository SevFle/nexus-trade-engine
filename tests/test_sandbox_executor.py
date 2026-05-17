"""Tests for the PluginSandboxExecutor with hardened isolation and trust enforcement."""

from __future__ import annotations

import asyncio

import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.executor import PluginSandboxExecutor
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.trust_levels import TrustLevel


class _GoodStrategy:
    name = "good_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


class _BadStrategy:
    name = "bad_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        raise RuntimeError("strategy crashed")


class _SlowStrategy:
    name = "slow_strategy"
    version = "1.0.0"

    async def on_bar(self, state, portfolio):
        await asyncio.sleep(60)
        return []


class _EmptyStrategy:
    name = "empty_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class TestPluginSandboxExecutorBasic:
    async def test_good_strategy_returns_signals(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    async def test_bad_strategy_returns_empty(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        executor = PluginSandboxExecutor(_BadStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_slow_strategy_times_out(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=1),
        )
        executor = PluginSandboxExecutor(_SlowStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_empty_strategy_returns_empty(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()


class TestPluginSandboxExecutorMetrics:
    async def test_metrics_collected_on_success(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "metrics_test")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("metrics_test")
            assert metrics is not None
            assert metrics["total_evaluations"] == 1
            assert metrics["total_signals_emitted"] == 1
        finally:
            executor.cleanup()

    async def test_metrics_accumulate_across_evaluations(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "accum_test")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            await executor.safe_evaluate(None, None, None)
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("accum_test")
            assert metrics["total_evaluations"] == 3
        finally:
            executor.cleanup()

    async def test_metrics_on_error(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="error_test",
            resource_policy=ResourcePolicy(max_cpu_seconds=1),
        )
        executor = PluginSandboxExecutor(_BadStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("error_test")
            assert metrics is not None
            assert metrics["errors"] == 1
        finally:
            executor.cleanup()


class TestPluginSandboxExecutorHealth:
    async def test_health_report(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "health_test")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        try:
            await executor.safe_evaluate(None, None, None)
            health = executor.get_health()
            assert health["strategy_name"] == "good_strategy"
            assert health["plugin_id"] == "health_test"
            assert health["trust_level"] == "untrusted"
            assert health["total_evaluations"] == 1
        finally:
            executor.cleanup()

    async def test_health_report_trusted(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "trusted_health")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            health = executor.get_health()
            assert health["trust_level"] == "trusted_full"
        finally:
            executor.cleanup()


class TestPluginSandboxExecutorFromFactory:
    def test_from_factory_basic(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "factory_test")

        def factory():
            return _EmptyStrategy()

        executor = PluginSandboxExecutor.from_factory(factory, policy)
        assert executor.strategy.name == "empty_strategy"
        executor.cleanup()


class TestPluginSandboxExecutorTrustEnforcement:
    async def test_untrusted_policy_enforced(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "enforce_test")
        assert policy.trust_level == "untrusted"
        assert policy.filesystem_policy.read_write_paths == []
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_trusted_full_policy_enforced(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "trusted_test")
        assert policy.trust_level == "trusted_full"
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_policy_integrity_preserved(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "integrity_test")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            await executor.safe_evaluate(None, None, None)
            assert executor.policy.verify_integrity() is True
        finally:
            executor.cleanup()
