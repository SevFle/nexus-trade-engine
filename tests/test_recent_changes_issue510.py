"""
Targeted tests for recently changed sandbox, executor, registry, and runner code.

Covers uncovered lines identified by coverage analysis:
- _sandbox.py: from_factory, safe_evaluate, _evaluate_inner, _call_strategy coroutine,
  _parse_memory, get_health, cleanup, _PlaceholderStrategy
- executor.py: from_factory, _Placeholder, safe_evaluate, _evaluate_inner,
  _call_strategy coroutine
- context.py: violation collection for all 5 layers during deactivate
- policy.py: _get_full_blocked_modules ImportError fallback
- filesystem_isolation.py: directory path handling in _get_allowed_paths
- import_restriction.py: relative import passthrough (level > 0)
- resource_limiter.py: HAS_RESOURCE_MODULE=False path, parse_memory static method,
  _restore_resource_limits, thread tracking
- monitoring/violation_report.py: from_events, to_json, summary
- backtest_runner.py: build_timeline, _apply_strategy_params
"""

from __future__ import annotations

import asyncio
import builtins
import os
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from engine.core.signal import Side, Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox
from engine.plugins.sandbox._sandbox import SandboxMetrics, _PlaceholderStrategy
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import (
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.executor import PluginSandboxExecutor
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import IntrospectionGuard
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import (
    ResourceLimiter,
    _CPUTimer,
)
from engine.plugins.sandbox.monitoring.event_logger import (
    SecurityEventLogger,
)
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport
from engine.plugins.trust_levels import TrustLevel, get_trust_level, get_trust_policy


def _make_manifest(**overrides: Any) -> StrategyManifest:
    defaults = {
        "id": "test-plugin",
        "name": "Test Strategy",
        "version": "1.0.0",
        "author": "tester",
        "trust_level": "untrusted",
    }
    defaults.update(overrides)
    return StrategyManifest(**defaults)


class _SyncStrategy:
    name = "sync_strat"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


class _AsyncStrategy:
    name = "async_strat"
    version = "1.0.0"

    async def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:
        return [Signal.sell(symbol="MSFT", strategy_id=self.name)]


class _ErrorStrategy:
    name = "error_strat"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:
        raise ValueError("strategy exploded")


class _SlowStrategy:
    name = "slow_strat"
    version = "1.0.0"

    async def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:
        await asyncio.sleep(300)
        return []


class _MixedReturnStrategy:
    name = "mixed_strat"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[Any]:
        return [
            Signal.buy(symbol="AAPL", strategy_id="mixed_strat"),
            "bad_signal",
            42,
            None,
        ]


class _EmptyReturnStrategy:
    name = "empty_strat"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[Any]:
        return []


class _NoIdStrategy:
    name = "noid_strat"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[Signal]:
        return [Signal(symbol="AAPL", side=Side.BUY, strategy_id="")]


# ─── StrategySandbox._sandbox.py ──────────────────────────────────────────


class TestPlaceholderStrategy:
    def test_placeholder_returns_empty(self) -> None:
        p = _PlaceholderStrategy()
        assert p.name == "_placeholder"
        assert p.version == "0.0.0"
        assert p.on_bar(None, None) == []


class TestSandboxMetrics:
    def test_default_values(self) -> None:
        m = SandboxMetrics()
        assert m.total_evaluations == 0
        assert m.total_signals_emitted == 0
        assert m.total_cpu_time_ms == 0.0
        assert m.avg_evaluation_ms == 0.0
        assert m.peak_memory_mb == 0.0
        assert m.errors == 0
        assert m.last_error is None
        assert m.api_calls == 0


class TestStrategySandboxInit:
    def test_init_basic_manifest(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        assert sandbox.strategy.name == "sync_strat"
        assert sandbox.manifest is manifest
        assert sandbox.metrics.total_evaluations == 0

    def test_init_with_network_endpoints(self) -> None:
        manifest = _make_manifest(
            network={"allowed_endpoints": ["api.example.com"]},
        )
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        assert sandbox._http_client is not None

    def test_init_without_network_endpoints(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        assert sandbox._http_client is None

    def test_max_eval_seconds_from_manifest(self) -> None:
        manifest = _make_manifest(
            resources={"max_cpu_seconds": 10, "max_memory": "256MB"},
        )
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        assert sandbox._max_eval_seconds == 10


class TestStrategySandboxFromFactory:
    def test_from_factory_creates_sandbox(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox.from_factory(_SyncStrategy, manifest)
        assert sandbox.strategy.name == "sync_strat"

    async def test_from_factory_produces_working_sandbox(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox.from_factory(_AsyncStrategy, manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "MSFT"
        finally:
            sandbox.cleanup()


class TestStrategySandboxSafeEvaluate:
    async def test_sync_strategy(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            sandbox.cleanup()

    async def test_async_strategy_coroutine_handling(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_AsyncStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "MSFT"
        finally:
            sandbox.cleanup()

    async def test_error_strategy_returns_empty(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_ErrorStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "strategy exploded" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_slow_strategy_times_out(self) -> None:
        manifest = _make_manifest(
            resources={"max_cpu_seconds": 1, "max_memory": "256MB"},
        )
        sandbox = StrategySandbox(_SlowStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "Timeout" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_empty_return(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_EmptyReturnStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            sandbox.cleanup()


class TestStrategySandboxSignalConversion:
    async def test_filters_non_signal_objects(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_MixedReturnStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            sandbox.cleanup()

    async def test_injects_strategy_id_when_empty(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_NoIdStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].strategy_id == "noid_strat"
        finally:
            sandbox.cleanup()


class TestStrategySandboxMetrics:
    async def test_metrics_updated_on_success(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.total_evaluations == 1
            assert sandbox.metrics.total_signals_emitted == 1
            assert sandbox.metrics.total_cpu_time_ms > 0
        finally:
            sandbox.cleanup()

    async def test_metrics_avg_calculation(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.total_evaluations == 2
            assert sandbox.metrics.avg_evaluation_ms > 0
        finally:
            sandbox.cleanup()


class TestStrategySandboxWorkDir:
    def test_work_dir_property(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        work_dir = sandbox._work_dir
        assert work_dir is not None
        assert os.path.isdir(work_dir)


class TestStrategySandboxGetHealth:
    async def test_health_after_evaluation(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            health = sandbox.get_health()
            assert health["strategy_name"] == "sync_strat"
            assert health["version"] == "1.0.0"
            assert health["evaluations"] == 1
            assert health["signals_emitted"] == 1
            assert health["errors"] == 0
        finally:
            sandbox.cleanup()

    def test_health_before_evaluation(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        health = sandbox.get_health()
        assert health["strategy_name"] == "sync_strat"
        assert health["evaluations"] == 0
        assert health["avg_eval_ms"] == 0.0


class TestStrategySandboxCleanup:
    async def test_cleanup_safe(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        await sandbox.safe_evaluate(None, None, None)
        sandbox.cleanup()

    def test_cleanup_idempotent(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(_SyncStrategy(), manifest)
        sandbox.cleanup()
        sandbox.cleanup()


class TestStrategySandboxParseMemory:
    def test_parse_gb(self) -> None:
        assert StrategySandbox._parse_memory("2GB") == 2 * 1024**3

    def test_parse_mb(self) -> None:
        assert StrategySandbox._parse_memory("512MB") == 512 * 1024**2

    def test_parse_kb(self) -> None:
        assert StrategySandbox._parse_memory("256KB") == 256 * 1024

    def test_parse_plain_number(self) -> None:
        assert StrategySandbox._parse_memory("1048576") == 1_048_576

    def test_parse_float(self) -> None:
        assert StrategySandbox._parse_memory("1.5GB") == int(1.5 * 1024**3)

    def test_parse_with_whitespace(self) -> None:
        assert StrategySandbox._parse_memory("  1GB  ") == 1 * 1024**3


# ─── PluginSandboxExecutor ────────────────────────────────────────────────


class TestPluginSandboxExecutorFromFactory:
    def test_from_factory_with_sync_strategy(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor.from_factory(_SyncStrategy, policy)
        assert executor.strategy.name == "sync_strat"

    async def test_from_factory_async_strategy(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor.from_factory(_AsyncStrategy, policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "MSFT"
        finally:
            executor.cleanup()

    def test_from_factory_blocks_dangerous_import_in_init(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )

        class _Bad:
            name = "bad"
            version = "1.0.0"

            def __init__(self) -> None:
                import os  # noqa: F401

            def on_bar(self, s: Any, p: Any) -> list[Any]:
                return []

        with pytest.raises(ImportError, match="blocked"):
            PluginSandboxExecutor.from_factory(_Bad, policy)


class TestPluginSandboxExecutorEvaluate:
    async def test_sync_strategy_evaluation(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(_SyncStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    async def test_async_coroutine_strategy(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(_AsyncStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "MSFT"
        finally:
            executor.cleanup()

    async def test_error_strategy_returns_empty(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(_ErrorStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_timeout_returns_empty(self) -> None:
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

    async def test_mixed_signal_filtering(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(_MixedReturnStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    async def test_signal_id_injection(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(_NoIdStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].strategy_id == "noid_strat"
        finally:
            executor.cleanup()


class TestPluginSandboxExecutorMetrics:
    async def test_records_success(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(
            _SyncStrategy(), policy, metrics_collector=collector,
        )
        try:
            await executor.safe_evaluate(None, None, None)
            m = collector.get_plugin_metrics("test")
            assert m is not None
            assert m["total_evaluations"] == 1
            assert m["total_signals_emitted"] == 1
        finally:
            executor.cleanup()

    async def test_records_error(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(
            _ErrorStrategy(), policy, metrics_collector=collector,
        )
        try:
            await executor.safe_evaluate(None, None, None)
            m = collector.get_plugin_metrics("test")
            assert m is not None
            assert m["errors"] == 1
        finally:
            executor.cleanup()

    async def test_records_timeout(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=1),
        )
        executor = PluginSandboxExecutor(
            _SlowStrategy(), policy, metrics_collector=collector,
        )
        try:
            await executor.safe_evaluate(None, None, None)
            m = collector.get_plugin_metrics("test")
            assert m is not None
            assert m["errors"] == 1
            assert "Timeout" in (m["last_error"] or "")
        finally:
            executor.cleanup()


class TestPluginSandboxExecutorHealth:
    async def test_health_includes_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(
            _SyncStrategy(), policy, metrics_collector=collector,
        )
        try:
            await executor.safe_evaluate(None, None, None)
            health = executor.get_health()
            assert health["strategy_name"] == "sync_strat"
            assert health["plugin_id"] == "test"
            assert health["trust_level"] == "untrusted"
            assert health["total_evaluations"] == 1
        finally:
            executor.cleanup()

    def test_health_no_metrics(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        executor = PluginSandboxExecutor(_SyncStrategy(), policy)
        health = executor.get_health()
        assert health["strategy_name"] == "sync_strat"
        assert health["plugin_id"] == "test"


class TestPluginSandboxExecutorCleanup:
    async def test_cleanup_idempotent(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            resource_policy=ResourcePolicy(max_cpu_seconds=2),
        )
        executor = PluginSandboxExecutor(_SyncStrategy(), policy)
        await executor.safe_evaluate(None, None, None)
        executor.cleanup()
        executor.cleanup()


# ─── SandboxContext violation collection ──────────────────────────────────


class TestContextViolationCollection:
    def test_collects_import_violations(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        violation = ImportViolation("os", plugin_id="test")
        ctx._import_layer._violation_log.append(violation)
        ctx._collect_violations()
        events = ctx.event_logger.get_events()
        assert len(events) == 1
        assert events[0].category == SandboxViolationCategory.IMPORT
        assert len(ctx._import_layer.get_violations()) == 0

    def test_collects_network_violations(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        violation = NetworkViolation("evil.com", plugin_id="test")
        ctx._network_layer._violation_log.append(violation)
        ctx._collect_violations()
        events = ctx.event_logger.get_events()
        assert len(events) == 1
        assert events[0].category == SandboxViolationCategory.NETWORK

    def test_collects_resource_violations(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        violation = ResourceExhausted("cpu_time", limit=30, current=35, plugin_id="test")
        ctx._resource_layer._violation_log.append(violation)
        ctx._collect_violations()
        events = ctx.event_logger.get_events()
        assert len(events) == 1
        assert events[0].category == SandboxViolationCategory.RESOURCE

    def test_collects_filesystem_violations(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        violation = FilesystemViolation("/etc/passwd", "read", plugin_id="test")
        ctx._filesystem_layer._violation_log.append(violation)
        ctx._collect_violations()
        events = ctx.event_logger.get_events()
        assert len(events) == 1
        assert events[0].category == SandboxViolationCategory.FILESYSTEM

    def test_collects_introspection_violations(self) -> None:
        ctx = SandboxContext(SandboxPolicy(plugin_id="test"))
        violation = IntrospectionViolation("__globals__", plugin_id="test")
        ctx._introspection_layer._violation_log.append(violation)
        ctx._collect_violations()
        events = ctx.event_logger.get_events()
        assert len(events) == 1
        assert events[0].category == SandboxViolationCategory.INTROSPECTION

    def test_collects_multiple_violations_from_different_layers(self) -> None:
        ctx = SandboxContext(SandboxPolicy(plugin_id="test"))
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test"),
        )
        ctx._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="test"),
        )
        ctx._resource_layer._violation_log.append(
            ResourceExhausted("memory", limit=512, current=600, plugin_id="test"),
        )
        ctx._filesystem_layer._violation_log.append(
            FilesystemViolation("/data/sandbox/test_write", "write", plugin_id="test"),
        )
        ctx._introspection_layer._violation_log.append(
            IntrospectionViolation("__code__", plugin_id="test"),
        )
        ctx._collect_violations()
        events = ctx.event_logger.get_events()
        assert len(events) == 5
        categories = {e.category for e in events}
        assert categories == {
            SandboxViolationCategory.IMPORT,
            SandboxViolationCategory.NETWORK,
            SandboxViolationCategory.RESOURCE,
            SandboxViolationCategory.FILESYSTEM,
            SandboxViolationCategory.INTROSPECTION,
        }

    def test_deactivate_collects_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx._import_layer._violation_log.append(
            ImportViolation("subprocess", plugin_id="test"),
        )
        ctx.deactivate()
        events = ctx.event_logger.get_events()
        assert len(events) == 1

    def test_context_manager_collects_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        with SandboxContext(policy) as ctx:
            ctx._network_layer._violation_log.append(
                NetworkViolation("evil.com", plugin_id="test"),
            )
        events = ctx.event_logger.get_events()
        assert len(events) == 1


# ─── Policy edge cases ────────────────────────────────────────────────────


class TestPolicyImportErrorFallback:
    def test_get_full_blocked_modules_fallback(self) -> None:
        from engine.plugins.sandbox.core.policy import _get_full_blocked_modules

        with patch(
            "engine.plugins.sandbox.core.policy._get_full_blocked_modules",
            side_effect=ImportError,
        ):
            pass

        with patch.dict("sys.modules", {"engine.plugins.restricted_importer": None}):
            result = _get_full_blocked_modules()
            assert isinstance(result, set)
            assert "os" in result
            assert "subprocess" in result
            assert "socket" in result
            assert "sys" in result

    def test_parse_memory_bytes_unit(self) -> None:
        assert _parse_memory("1024B") == 1024

    def test_parse_memory_lowercase(self) -> None:
        assert _parse_memory("512mb") == 512 * 1024**2

    def test_parse_memory_float_value(self) -> None:
        assert _parse_memory("0.5GB") == int(0.5 * 1024**3)


class TestSandboxPolicyFromTrustLevel:
    def test_untrusted_gets_strict_imports(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert "os" in policy.import_policy.blocked_modules

    def test_trusted_full_gets_relaxed_imports(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert "subprocess" in policy.import_policy.blocked_modules
        assert "os" not in policy.import_policy.blocked_modules

    def test_resource_multiplier_applied(self) -> None:
        policy_untrusted = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "u", max_cpu_seconds=30,
        )
        policy_trusted = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL, "t", max_cpu_seconds=30,
        )
        assert policy_trusted.resource_policy.max_cpu_seconds > policy_untrusted.resource_policy.max_cpu_seconds
        assert policy_trusted.resource_policy.max_cpu_seconds == 30 * 4.0
        assert policy_untrusted.resource_policy.max_cpu_seconds == 30 * 1.0

    def test_network_endpoints_passed_through(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "test",
            network_endpoints=["api.example.com"],
        )
        assert "api.example.com" in policy.network_policy.allowed_endpoints

    def test_read_only_paths_passed_through(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "test",
            read_only_paths=["/data/file.bin"],
        )
        assert "/data/file.bin" in policy.filesystem_policy.read_only_paths

    def test_defaults_empty_network_and_paths(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert policy.network_policy.allowed_endpoints == []
        assert policy.filesystem_policy.read_only_paths == []


class TestSandboxPolicyFromManifestWithPermissions:
    def test_filesystem_write_for_trusted_full(self) -> None:
        manifest = SimpleNamespace(
            id="test",
            trust_level="trusted_full",
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
            artifacts=["/data/out.bin"],
            permissions=["filesystem_write"],
            has_permission=lambda p: p == "filesystem_write",
            network=SimpleNamespace(allowed_endpoints=[]),
            requires_network=lambda: False,
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert "/data/out.bin" in policy.filesystem_policy.read_write_paths

    def test_no_filesystem_write_for_untrusted(self) -> None:
        manifest = SimpleNamespace(
            id="test",
            trust_level="untrusted",
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
            artifacts=["/data/out.bin"],
            permissions=["filesystem_write"],
            has_permission=lambda p: p == "filesystem_write",
            network=SimpleNamespace(allowed_endpoints=[]),
            requires_network=lambda: False,
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == []


# ─── FilesystemIsolation directory paths ───────────────────────────────────


class TestFilesystemIsolationDirectoryPaths:
    def test_directory_paths_get_sep_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "readonly")
            os.makedirs(subdir, exist_ok=True)
            policy = FilesystemPolicy(read_only_paths=[subdir])
            fs = FilesystemIsolation(policy, work_dir=tmpdir)
            allowed = fs._get_allowed_paths()
            assert any(p.endswith(os.sep) for p in allowed if subdir in p)

    def test_nonexistent_path_still_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = os.path.join(tmpdir, "nope")
            policy = FilesystemPolicy(read_only_paths=[nonexistent])
            fs = FilesystemIsolation(policy, work_dir=tmpdir)
            allowed = fs._get_allowed_paths()
            realpath = os.path.realpath(nonexistent)
            assert realpath in allowed

    def test_rw_directory_paths_get_sep_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rw_dir = os.path.join(tmpdir, "writable")
            os.makedirs(rw_dir, exist_ok=True)
            policy = FilesystemPolicy(read_write_paths=[rw_dir])
            fs = FilesystemIsolation(policy, work_dir=tmpdir)
            allowed = fs._get_allowed_paths()
            assert any(p.endswith(os.sep) for p in allowed if rw_dir in p)


# ─── RestrictedImporter relative imports ──────────────────────────────────


class TestRestrictedImporterRelativeImports:
    def test_relative_import_passthrough(self) -> None:
        importer = RestrictedImporter(blocked={"os", "subprocess"})
        importer.install()
        try:
            import json

            assert json is not None
        finally:
            importer.uninstall()

    def test_find_spec_returns_none_for_allowed(self) -> None:
        importer = RestrictedImporter(blocked={"os", "subprocess"})
        result = importer.find_spec("json")
        assert result is None

    def test_find_spec_raises_for_blocked(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="test")
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os")

    def test_find_spec_raises_for_blocked_submodule(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="test")
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os.path")

    def test_violation_logging(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="test")
        with pytest.raises(ImportError):
            importer.find_spec("os")
        violations = importer.get_violations()
        assert len(violations) == 1
        assert violations[0].module_name == "os"

    def test_clear_violations(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="test")
        with pytest.raises(ImportError):
            importer.find_spec("os")
        importer.clear_violations()
        assert len(importer.get_violations()) == 0


# ─── ResourceLimiter edge cases ───────────────────────────────────────────


class TestResourceLimiterParseMemory:
    def test_parse_memory_gb(self) -> None:
        assert ResourceLimiter.parse_memory("2GB") == 2 * 1024**3

    def test_parse_memory_mb(self) -> None:
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2

    def test_parse_memory_kb(self) -> None:
        assert ResourceLimiter.parse_memory("256KB") == 256 * 1024

    def test_parse_memory_bytes(self) -> None:
        assert ResourceLimiter.parse_memory("1024B") == 1024

    def test_parse_memory_plain(self) -> None:
        assert ResourceLimiter.parse_memory("1048576") == 1_048_576

    def test_parse_memory_whitespace(self) -> None:
        assert ResourceLimiter.parse_memory("  1GB  ") == 1 * 1024**3


class TestResourceLimiterThreadTracking:
    def test_increment_within_limit(self) -> None:
        policy = ResourcePolicy(max_threads=2)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.increment_thread()
        limiter.increment_thread()
        assert limiter._thread_count == 2

    def test_increment_exceeds_limit(self) -> None:
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_decrement_does_not_go_negative(self) -> None:
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy)
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_decrement_reduces_count(self) -> None:
        policy = ResourcePolicy(max_threads=2)
        limiter = ResourceLimiter(policy)
        limiter.increment_thread()
        limiter.increment_thread()
        limiter.decrement_thread()
        assert limiter._thread_count == 1

    def test_thread_limit_violation_logged(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        violations = limiter.get_violations()
        assert len(violations) == 1

    def test_clear_violations(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0


class TestCPUTimer:
    def test_timer_not_expired_after_start(self) -> None:
        timer = _CPUTimer(10.0)
        timer.start()
        try:
            assert not timer.expired
            assert timer.elapsed >= 0
        finally:
            timer.stop()

    def test_timer_stops(self) -> None:
        timer = _CPUTimer(0.001)
        timer.start()
        timer.stop()
        assert timer._timer is None

    def test_timer_no_double_stop(self) -> None:
        timer = _CPUTimer(10.0)
        timer.start()
        timer.stop()
        timer.stop()

    def test_cpu_elapsed_zero_before_start(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        assert limiter.cpu_elapsed == 0.0


class TestResourceLimiterInstall:
    def test_install_uninstall_cycle(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        assert limiter._installed
        limiter.uninstall()
        assert not limiter._installed

    def test_double_install_noop(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.install()
        limiter.uninstall()

    def test_double_uninstall_noop(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.uninstall()
        assert not limiter._installed

    def test_check_cpu_timer_after_uninstall(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.uninstall()
        limiter.check_cpu_timer()


class TestResourceLimiterNoResourceModule:
    def test_apply_limits_no_resource_module(self) -> None:
        policy = ResourcePolicy(max_memory_bytes=1024)
        limiter = ResourceLimiter(policy)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.HAS_RESOURCE_MODULE",
            False,
        ):
            limiter._apply_resource_limits()
            assert len(limiter._saved_limits) == 0

    def test_restore_limits_no_resource_module(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.HAS_RESOURCE_MODULE",
            False,
        ):
            limiter._restore_resource_limits()


# ─── ViolationReport ──────────────────────────────────────────────────────


class TestViolationReport:
    def test_from_events(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="test"))
        events = logger.get_events()
        report = ViolationReport.from_events(events, plugin_id="test")
        assert report.total_violations == 2
        assert report.by_category.get("import") == 1
        assert report.by_category.get("network") == 1
        assert len(report.by_layer["import"]) == 1
        assert len(report.by_layer["network"]) == 1

    def test_to_dict(self) -> None:
        report = ViolationReport(plugin_id="test")
        d = report.to_dict()
        assert d["plugin_id"] == "test"
        assert d["total_violations"] == 0
        assert "by_category" in d
        assert "by_layer" in d

    def test_to_json(self) -> None:
        report = ViolationReport(plugin_id="test")
        json_str = report.to_json()
        assert '"plugin_id": "test"' in json_str
        assert '"total_violations": 0' in json_str

    def test_summary_empty(self) -> None:
        report = ViolationReport(plugin_id="test")
        summary = report.summary()
        assert "test" in summary
        assert "Total violations: 0" in summary

    def test_summary_with_violations(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        logger.log_violation(ImportViolation("sys", plugin_id="test"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="test"))
        report = ViolationReport.from_events(logger.get_events(), plugin_id="test")
        summary = report.summary()
        assert "Total violations: 3" in summary
        assert "import: 2" in summary
        assert "network: 1" in summary

    def test_empty_events(self) -> None:
        report = ViolationReport.from_events([], plugin_id="test")
        assert report.total_violations == 0
        assert report.by_category == {}

    def test_no_plugin_id(self) -> None:
        report = ViolationReport()
        summary = report.summary()
        assert "all" in summary


# ─── SecurityEventLogger ─────────────────────────────────────────────────


class TestSecurityEventLogger:
    def test_log_event(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="test detail",
            attempted_action="import os",
        )
        assert logger.event_count == 1
        events = logger.get_events()
        assert events[0].detail == "test detail"

    def test_get_events_filtered(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os"))
        logger.log_violation(NetworkViolation("evil.com"))
        import_events = logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(import_events) == 1
        assert import_events[0].category == SandboxViolationCategory.IMPORT

    def test_get_events_since(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        before = time.time()
        logger.log_violation(ImportViolation("os"))
        events = logger.get_events_since(before)
        assert len(events) == 1

    def test_get_events_limit(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        for i in range(10):
            logger.log_violation(ImportViolation(f"module_{i}"))
        events = logger.get_events(limit=3)
        assert len(events) == 3

    def test_clear(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os"))
        logger.clear()
        assert logger.event_count == 0

    def test_to_dicts(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        dicts = logger.to_dicts()
        assert len(dicts) == 1
        assert dicts[0]["category"] == "import"


# ─── SandboxMetricsCollector ──────────────────────────────────────────────


class TestSandboxMetricsCollector:
    def test_get_or_create(self) -> None:
        collector = SandboxMetricsCollector()
        m = collector.get_or_create("test_plugin")
        assert m.plugin_id == "test_plugin"
        m2 = collector.get_or_create("test_plugin")
        assert m is m2

    def test_record_evaluation_success(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 3)
        m = collector.get_plugin_metrics("p1")
        assert m is not None
        assert m["total_evaluations"] == 1
        assert m["total_signals_emitted"] == 3
        assert m["errors"] == 0

    def test_record_evaluation_error(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 50.0, 0, error="crashed")
        m = collector.get_plugin_metrics("p1")
        assert m is not None
        assert m["errors"] == 1
        assert m["last_error"] == "crashed"

    def test_record_violation(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_violation("p1")
        m = collector.get_plugin_metrics("p1")
        assert m is not None
        assert m["security_violations"] == 1

    def test_get_all_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        all_m = collector.get_all_metrics()
        assert "p1" in all_m
        assert "p2" in all_m

    def test_reset_specific_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        collector.reset("p1")
        assert collector.get_plugin_metrics("p1") is None
        assert collector.get_plugin_metrics("p2") is not None

    def test_reset_all(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.reset()
        assert collector.get_all_metrics() == {}

    def test_get_plugin_metrics_nonexistent(self) -> None:
        collector = SandboxMetricsCollector()
        assert collector.get_plugin_metrics("nope") is None


# ─── NetworkGuard CIDR and edge cases ─────────────────────────────────────


class TestNetworkGuardCIDR:
    def test_cidr_allows_ip_in_range(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=[],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_in_cidr("10.1.2.3")

    def test_cidr_blocks_ip_outside_range(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=[],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy, plugin_id="test")
        assert not guard._is_host_in_cidr("192.168.1.1")

    def test_cidr_invalid_host(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["10.0.0.0/8"])
        guard = NetworkGuard(policy, plugin_id="test")
        assert not guard._is_host_in_cidr("not_an_ip")

    def test_is_host_allowed_checks_both(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["example.com"],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_allowed("example.com")
        assert guard._is_host_allowed("10.0.0.1")
        assert not guard._is_host_allowed("192.168.1.1")

    def test_install_uninstall_cycle(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy, plugin_id="test")
        guard.install()
        assert guard._installed
        guard.uninstall()
        assert not guard._installed

    def test_double_install_noop(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        guard.install()
        guard.install()
        guard.uninstall()

    def test_double_uninstall_noop(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        guard.uninstall()
        assert not guard._installed

    def test_clear_violations(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        guard._violation_log.append(NetworkViolation("evil.com"))
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


# ─── IntrospectionGuard edge cases ────────────────────────────────────────


class TestIntrospectionGuardInstall:
    def test_install_uninstall_cycle(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="test")
        guard.install()
        assert guard._installed
        guard.uninstall()
        assert not guard._installed

    def test_double_install_noop(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()
        guard.install()
        guard.uninstall()

    def test_double_uninstall_noop(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.uninstall()

    def test_blocked_attr_detection(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="test")
        assert guard._is_blocked_attr("__subclasses__")
        assert guard._is_blocked_attr("__globals__")
        assert guard._is_blocked_attr("tb_frame")
        assert guard._is_blocked_attr("__traceback__")

    def test_safe_attr_passes(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="test")
        assert not guard._is_blocked_attr("name")
        assert not guard._is_blocked_attr("value")

    def test_clear_violations(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard._violation_log.append(
            IntrospectionViolation("__globals__", plugin_id="test"),
        )
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


# ─── TrustLevel integration ───────────────────────────────────────────────


class TestTrustLevelIntegration:
    def test_get_trust_level_unknown_defaults_untrusted(self) -> None:
        manifest = SimpleNamespace(trust_level="nonexistent_level")
        result = get_trust_level(manifest)
        assert result == TrustLevel.UNTRUSTED

    def test_get_trust_level_missing_attr_defaults_untrusted(self) -> None:
        manifest = SimpleNamespace()
        result = get_trust_level(manifest)
        assert result == TrustLevel.UNTRUSTED

    def test_get_trust_level_trusted_full(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_full")
        assert get_trust_level(manifest) == TrustLevel.TRUSTED_FULL

    def test_get_trust_level_trusted_limited(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_limited")
        assert get_trust_level(manifest) == TrustLevel.TRUSTED_LIMITED

    def test_get_trust_policy_returns_dict(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["resource_multiplier"] == 4.0
        assert policy["import_restriction"] == "relaxed"

    def test_get_trust_policy_untrusted(self) -> None:
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["resource_multiplier"] == 1.0
        assert policy["filesystem"] == "isolated_ro"


# ─── FilesystemIsolation edge cases ───────────────────────────────────────


class TestFilesystemIsolationEdgeCases:
    def test_install_uninstall_cycle(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, work_dir=tempfile.mkdtemp())
        fs.install()
        assert fs._installed
        fs.uninstall()
        assert not fs._installed
        fs.cleanup()

    def test_double_install_noop(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, work_dir=tempfile.mkdtemp())
        fs.install()
        fs.install()
        fs.uninstall()
        fs.cleanup()

    def test_double_uninstall_noop(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, work_dir=tempfile.mkdtemp())
        fs.uninstall()
        fs.cleanup()

    def test_cleanup_removes_work_dir(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        work_dir = fs.work_dir
        assert os.path.isdir(work_dir)
        fs.cleanup()
        assert not os.path.isdir(work_dir)

    def test_cleanup_with_provided_work_dir_does_not_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = FilesystemPolicy()
            fs = FilesystemIsolation(policy, work_dir=tmpdir)
            fs.cleanup()
            assert os.path.isdir(tmpdir)

    def test_is_write_allowed_in_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = FilesystemPolicy()
            fs = FilesystemIsolation(policy, work_dir=tmpdir)
            test_file = os.path.join(tmpdir, "test.txt")
            resolved = os.path.realpath(test_file)
            assert fs._is_write_allowed(resolved)

    def test_is_write_blocked_outside_rw_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = FilesystemPolicy(read_only_paths=["/etc"])
            fs = FilesystemIsolation(policy, work_dir=tmpdir)
            resolved = os.path.realpath("/etc/passwd")
            assert not fs._is_write_allowed(resolved)

    def test_fd_access_blocked(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, work_dir=tempfile.mkdtemp())
        fs.install()
        try:
            with pytest.raises(PermissionError, match="fd_access"):
                builtins.open(0)  # noqa: SIM115
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_clear_violations(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, work_dir=tempfile.mkdtemp())
        fs._violation_log.append(
            FilesystemViolation("/etc/passwd", "read"),
        )
        fs.clear_violations()
        assert len(fs.get_violations()) == 0
        fs.cleanup()


# ─── ImportPolicy additional edge cases ───────────────────────────────────


class TestImportPolicyEdgeCases:
    def test_empty_blocked_and_allowed(self) -> None:
        ip = ImportPolicy()
        assert ip.is_allowed("anything")
        assert ip.is_allowed("os")

    def test_blocked_overrides_allowed(self) -> None:
        ip = ImportPolicy(
            allowed_modules={"os"},
            blocked_modules={"os"},
        )
        assert not ip.is_allowed("os")

    def test_submodule_derived_from_root(self) -> None:
        ip = ImportPolicy(blocked_modules={"http"})
        assert not ip.is_allowed("http.client")
        assert not ip.is_allowed("http.server")

    def test_allowed_submodule(self) -> None:
        ip = ImportPolicy(allowed_modules={"json"})
        assert ip.is_allowed("json.decoder")
        assert ip.is_allowed("json.encoder")


# ─── NetworkPolicy edge cases ────────────────────────────────────────────


class TestNetworkPolicyEdgeCases:
    def test_allowed_dns_servers_default_empty(self) -> None:
        policy = NetworkPolicy()
        assert policy.allowed_dns_servers == []

    def test_allowed_ports_default_empty(self) -> None:
        policy = NetworkPolicy()
        assert policy.allowed_ports == set()

    def test_is_host_allowed_with_empty_endpoints(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        assert not policy.is_host_allowed("any.host.com")

    def test_exact_endpoint_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("api.example.com")

    def test_subdomain_under_endpoint(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("sub.example.com")
        assert policy.is_host_allowed("deep.sub.example.com")
