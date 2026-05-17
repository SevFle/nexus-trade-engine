"""
Integration tests for the complete sandboxed plugin system.

Tests the full pipeline: PluginRegistry discovery -> sandboxed loading ->
strategy execution, with TrustLevel flowing through from manifest to sandbox
configuration.

Covers:
  - Registry loads strategies via sandbox when use_sandbox=True
  - TrustLevel from manifest reaches SandboxPolicy
  - Different trust levels produce different policy constraints
  - PluginSigner integrity verification gates sandboxed loading
  - Full end-to-end: discover -> load -> evaluate -> cleanup
  - ViolationReport generation from sandboxed execution
"""

from __future__ import annotations

import builtins
import textwrap
from pathlib import Path

import pytest
import yaml

from engine.plugins.manifest import StrategyManifest
from engine.plugins.plugin_signing import PluginSigner
from engine.plugins.registry import PluginRegistry
from engine.plugins.sandbox import StrategySandbox
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    IntrospectionPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.executor import PluginSandboxExecutor
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport
from engine.plugins.trust_levels import TrustLevel, get_trust_level, get_trust_policy


def _write_strategy(directory: Path, manifest: dict, code: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.yaml").open("w") as f:
        yaml.dump(manifest, f)
    (directory / "strategy.py").write_text(code)


def _good_strategy_code() -> str:
    return textwrap.dedent("""\
        from engine.plugins.sdk import BaseStrategy

        class Strategy(BaseStrategy):
            name = "test_strat"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                return []
    """)


def _importing_strategy_code() -> str:
    return textwrap.dedent("""\
        class Strategy:
            name = "importing"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                import os
                return []
    """)


# ── TrustLevel -> SandboxPolicy Wiring ────────────────────────────────


class TestTrustLevelToPolicy:
    def test_untrusted_manifest_creates_strict_policy(self) -> None:
        manifest = StrategyManifest(
            id="untrusted_plugin",
            name="untrusted_plugin",
            version="1.0.0",
            trust_level="untrusted",
            resources={"max_cpu_seconds": 10, "max_memory": "256MB"},
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "untrusted"
        assert "os" in policy.import_policy.blocked_modules
        assert "subprocess" in policy.import_policy.blocked_modules
        assert "eval" in policy.introspection_policy.blocked_builtins
        assert "exec" in policy.introspection_policy.blocked_builtins

    def test_trusted_full_manifest_creates_relaxed_policy(self) -> None:
        manifest = StrategyManifest(
            id="trusted_plugin",
            name="trusted_plugin",
            version="1.0.0",
            trust_level="trusted_full",
            resources={"max_cpu_seconds": 30, "max_memory": "512MB"},
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "trusted_full"
        assert "os" not in policy.import_policy.blocked_modules
        assert "subprocess" in policy.import_policy.blocked_modules
        assert "ctypes" in policy.import_policy.blocked_modules

    def test_trusted_limited_manifest_creates_standard_policy(self) -> None:
        manifest = StrategyManifest(
            id="limited_plugin",
            name="limited_plugin",
            version="1.0.0",
            trust_level="trusted_limited",
            resources={"max_cpu_seconds": 30, "max_memory": "512MB"},
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "trusted_limited"
        assert "os" in policy.import_policy.blocked_modules

    def test_resource_multiplier_applied_for_trusted(self) -> None:
        manifest = StrategyManifest(
            id="trusted_plugin",
            name="trusted_plugin",
            version="1.0.0",
            trust_level="trusted_full",
            resources={"max_cpu_seconds": 30, "max_memory": "512MB"},
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.resource_policy.max_cpu_seconds == 30 * 4.0
        assert policy.resource_policy.max_memory_bytes == int(512 * 1024**2 * 4.0)

    def test_resource_multiplier_not_applied_for_untrusted(self) -> None:
        manifest = StrategyManifest(
            id="untrusted_plugin",
            name="untrusted_plugin",
            version="1.0.0",
            trust_level="untrusted",
            resources={"max_cpu_seconds": 30, "max_memory": "512MB"},
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.resource_policy.max_cpu_seconds == 30 * 1.0
        assert policy.resource_policy.max_memory_bytes == 512 * 1024**2

    def test_trusted_full_has_relaxed_introspection(self) -> None:
        manifest = StrategyManifest(
            id="trusted_plugin",
            name="trusted_plugin",
            version="1.0.0",
            trust_level="trusted_full",
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert "eval" not in policy.introspection_policy.blocked_builtins
        assert "exec" in policy.introspection_policy.blocked_builtins
        assert len(policy.introspection_policy.blocked_attributes) < len(
            IntrospectionPolicy().blocked_attributes
        )

    def test_invalid_trust_level_defaults_to_untrusted(self) -> None:
        manifest = StrategyManifest(
            id="bad_trust",
            name="bad_trust",
            version="1.0.0",
            trust_level="super_admin",
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "untrusted"
        assert "os" in policy.import_policy.blocked_modules

    def test_from_trust_level_classmethod(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL,
            plugin_id="direct",
            max_cpu_seconds=60,
        )
        assert policy.trust_level == "trusted_full"
        assert policy.plugin_id == "direct"
        assert policy.resource_policy.max_cpu_seconds == 60 * 4.0
        assert "subprocess" in policy.import_policy.blocked_modules

    def test_from_trust_level_untrusted(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            plugin_id="untrusted_direct",
        )
        assert policy.trust_level == "untrusted"
        assert "os" in policy.import_policy.blocked_modules


# ── get_trust_level Integration ───────────────────────────────────────


class TestGetTrustLevelIntegration:
    def test_manifest_with_trust_level(self) -> None:
        manifest = StrategyManifest(
            id="test", name="test", version="1.0", trust_level="trusted_full"
        )
        trust = get_trust_level(manifest)
        assert trust == TrustLevel.TRUSTED_FULL

    def test_manifest_default_untrusted(self) -> None:
        manifest = StrategyManifest(id="test", name="test", version="1.0")
        trust = get_trust_level(manifest)
        assert trust == TrustLevel.UNTRUSTED

    def test_trust_policy_dict_structure(self) -> None:
        for level in TrustLevel:
            policy = get_trust_policy(level)
            assert "import_restriction" in policy
            assert "resource_multiplier" in policy
            assert "filesystem" in policy
            assert "introspection" in policy


# ── PluginRegistry Sandbox Loading ────────────────────────────────────


class TestRegistrySandboxedLoading:
    def test_sandboxed_load_returns_executor(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "test_strat",
            {
                "id": "test_strat",
                "name": "test_strat",
                "version": "1.0.0",
                "trust_level": "untrusted",
            },
            _good_strategy_code(),
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        result = registry.load_strategy("test_strat")
        assert result is not None
        assert isinstance(result, PluginSandboxExecutor)
        result.cleanup()

    def test_sandboxed_load_with_trusted_full(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "trusted_strat",
            {
                "id": "trusted_strat",
                "name": "trusted_strat",
                "version": "1.0.0",
                "trust_level": "trusted_full",
            },
            _good_strategy_code(),
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        result = registry.load_strategy("trusted_strat")
        assert result is not None
        assert result.policy.trust_level == "trusted_full"
        assert "os" not in result.policy.import_policy.blocked_modules
        result.cleanup()

    def test_sandboxed_load_blocks_importing_strategy(self, tmp_path: Path) -> None:
        code = textwrap.dedent("""\
            class Strategy:
                name = "importing"
                version = "1.0.0"

                def __init__(self):
                    import os

                def on_bar(self, state, portfolio):
                    return []
        """)
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "importing",
            {
                "id": "importing",
                "name": "importing",
                "version": "1.0.0",
                "trust_level": "untrusted",
            },
            code,
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        result = registry.load_strategy("importing")
        assert result is None

    def test_sandboxed_load_with_integrity_check(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        code = _good_strategy_code()
        _write_strategy(
            strat_dir / "integrity_strat",
            {
                "id": "integrity_strat",
                "name": "integrity_strat",
                "version": "1.0.0",
                "content_hash": "wrong_hash",
            },
            code,
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        result = registry.load_strategy("integrity_strat")
        assert result is None

    def test_sandboxed_load_with_correct_hash(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        code = _good_strategy_code()
        strategy_path = strat_dir / "hash_strat"
        _write_strategy(
            strategy_path,
            {
                "id": "hash_strat",
                "name": "hash_strat",
                "version": "1.0.0",
            },
            code,
        )
        correct_hash = PluginSigner.compute_hash(str(strategy_path / "strategy.py"))
        (strategy_path / "manifest.yaml").write_text(
            yaml.dump({
                "id": "hash_strat",
                "name": "hash_strat",
                "version": "1.0.0",
                "content_hash": correct_hash,
            })
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        result = registry.load_strategy("hash_strat")
        assert result is not None
        result.cleanup()

    def test_non_sandboxed_load_still_works(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "normal",
            {"name": "normal", "version": "1.0.0"},
            _good_strategy_code(),
        )
        registry = PluginRegistry(strat_dir, use_sandbox=False)
        result = registry.load_strategy("normal")
        assert result is not None
        assert not isinstance(result, PluginSandboxExecutor)

    def test_manifest_defaults_populated(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "minimal",
            {"name": "minimal"},
            _good_strategy_code(),
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        result = registry.load_strategy("minimal")
        assert result is not None
        assert result.policy.plugin_id == "minimal"
        result.cleanup()


# ── Full Pipeline E2E ─────────────────────────────────────────────────


class TestFullPipelineE2E:
    async def test_sandboxed_strategy_produces_signals(self, tmp_path: Path) -> None:
        code = textwrap.dedent("""\
            from engine.core.signal import Signal

            class Strategy:
                name = "signal_strat"
                version = "1.0.0"

                def on_bar(self, state, portfolio):
                    return [Signal.buy(symbol="AAPL", strategy_id=self.name)]
        """)
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "signal_strat",
            {"id": "signal_strat", "name": "signal_strat", "version": "1.0.0"},
            code,
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        executor = registry.load_strategy("signal_strat")
        assert executor is not None
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    async def test_sandboxed_strategy_violation_returns_empty(self, tmp_path: Path) -> None:
        code = textwrap.dedent("""\
            class Strategy:
                name = "bad_strat"
                version = "1.0.0"

                def __init__(self):
                    import os

                def on_bar(self, state, portfolio):
                    return []
        """)
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "bad_strat",
            {"id": "bad_strat", "name": "bad_strat", "version": "1.0.0"},
            code,
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        assert registry.load_strategy("bad_strat") is None

    async def test_trusted_strategy_gets_relaxed_sandbox(self, tmp_path: Path) -> None:
        code = textwrap.dedent("""\
            import math

            class Strategy:
                name = "math_strat"
                version = "1.0.0"

                def on_bar(self, state, portfolio):
                    _ = math.sqrt(4)
                    return []
        """)
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "math_strat",
            {
                "id": "math_strat",
                "name": "math_strat",
                "version": "1.0.0",
                "trust_level": "trusted_full",
            },
            code,
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        executor = registry.load_strategy("math_strat")
        assert executor is not None
        assert executor.policy.trust_level == "trusted_full"
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_health_report_after_execution(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "health_strat",
            {"id": "health_strat", "name": "health_strat", "version": "1.0.0"},
            _good_strategy_code(),
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        executor = registry.load_strategy("health_strat")
        assert executor is not None
        try:
            await executor.safe_evaluate(None, None, None)
            health = executor.get_health()
            assert health["plugin_id"] == "health_strat"
            assert health["trust_level"] == "untrusted"
            assert health["total_evaluations"] == 1
        finally:
            executor.cleanup()

    async def test_builtins_restored_after_sandboxed_execution(self, tmp_path: Path) -> None:
        strat_dir = tmp_path / "strategies"
        _write_strategy(
            strat_dir / "restore_strat",
            {"id": "restore_strat", "name": "restore_strat", "version": "1.0.0"},
            _good_strategy_code(),
        )
        registry = PluginRegistry(strat_dir, use_sandbox=True)
        executor = registry.load_strategy("restore_strat")
        assert executor is not None

        orig_import = builtins.__import__
        orig_open = builtins.open
        orig_getattr = builtins.getattr
        try:
            await executor.safe_evaluate(None, None, None)
            assert builtins.__import__ is orig_import
            assert builtins.open is orig_open
            assert builtins.getattr is orig_getattr
        finally:
            executor.cleanup()


# ── StrategySandbox with TrustLevel ───────────────────────────────────


class TestStrategySandboxTrustLevel:
    async def test_sandbox_from_manifest_with_trust_level(self) -> None:
        manifest = StrategyManifest(
            id="trusted",
            name="trusted",
            version="1.0.0",
            trust_level="trusted_full",
            resources={"max_cpu_seconds": 5},
        )

        class Strat:
            name = "trusted"
            version = "1.0"

            def on_bar(self, s, p):
                return []

        sandbox = StrategySandbox(Strat(), manifest)
        try:
            assert sandbox._policy.trust_level == "trusted_full"
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            sandbox.cleanup()

    async def test_sandbox_policy_from_untrusted_manifest(self) -> None:
        manifest = StrategyManifest(
            id="untrusted",
            name="untrusted",
            version="1.0.0",
            trust_level="untrusted",
            resources={"max_cpu_seconds": 5},
        )

        class Strat:
            name = "untrusted"
            version = "1.0"

            def on_bar(self, s, p):
                return []

        sandbox = StrategySandbox(Strat(), manifest)
        try:
            assert sandbox._policy.trust_level == "untrusted"
            assert "os" in sandbox._policy.import_policy.blocked_modules
        finally:
            sandbox.cleanup()


# ── ViolationReport from Sandbox Execution ────────────────────────────


class TestViolationReportIntegration:
    async def test_report_from_sandbox_context_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "report_test")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
        finally:
            ctx.deactivate()

        events = ctx.event_logger.get_events()
        assert len(events) >= 1

        report = ViolationReport.from_events(events, plugin_id="report_test")
        assert report.total_violations >= 1
        assert report.by_category.get("import", 0) >= 1
        ctx.cleanup()

    def test_report_to_json_roundtrip(self) -> None:
        import json

        report = ViolationReport(plugin_id="test")
        report.by_category = {"import": 3, "network": 1}
        report.total_violations = 4
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["plugin_id"] == "test"
        assert parsed["total_violations"] == 4


# ── PluginMetrics with Executor ───────────────────────────────────────


class TestMetricsWithExecutor:
    async def test_metrics_collected_across_evaluations(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="metrics_test",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class Strat:
            name = "metrics_test"
            version = "1.0"

            def on_bar(self, s, p):
                return []

        executor = PluginSandboxExecutor(Strat(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("metrics_test")
            assert metrics is not None
            assert metrics["total_evaluations"] == 2
        finally:
            executor.cleanup()
