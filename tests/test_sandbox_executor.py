"""Tests for the PluginSandboxExecutor with hardened isolation and trust enforcement."""

from __future__ import annotations

import asyncio

import pytest

from engine.core.signal import Signal
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    ImportPolicy,
    IntrospectionPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import SandboxViolation
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
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test", max_cpu_seconds=1)
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
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "error_test", max_cpu_seconds=1)
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


class TestSandboxContextTrustValidation:
    def test_activate_raises_on_empty_blocked_modules(self) -> None:
        policy = SandboxPolicy(plugin_id="bad_trust")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()

    def test_activate_raises_on_cpu_exceeds_validate_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="cpu_validate_fail",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=90),
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()

    def test_activate_raises_on_read_write_paths_untrusted(self) -> None:
        from engine.plugins.sandbox.core.policy import FilesystemPolicy

        policy = SandboxPolicy(
            plugin_id="rw_paths_fail",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data/write"]),
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()

    def test_activate_raises_on_tampered_integrity(self) -> None:
        # from_trust_level() auto-sets _integrity_hash. We replace
        # introspection_policy with a fresh copy and re-set the hash so
        # verify_integrity() can detect subsequent mutations.
        #
        # NOTE: from_trust_level() passes the shared _TRUST_INTROSPECTION_PRESETS
        # object by reference. Mutating it would corrupt the preset for later
        # tests, so we replace introspection_policy with a fresh copy first.
        #
        # Tamper via introspection_policy.blocked_builtins (set to empty = inverted).
        # Choice rationale: blocked_builtins IS included in compute_integrity_hash()
        # (policy.py:209) but is NOT subject to validate_trust_level() resource
        # thresholds (context.py:88-94). Being in introspection_policy (a different
        # sub-object from resource_policy) makes it less likely to be independently
        # validated. This guarantees detection is purely via integrity hash mismatch.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_test")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()

        policy.introspection_policy.blocked_builtins = set()
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()


class TestSandboxContextHardLimits:
    def test_activate_raises_on_memory_hard_limit_exceeded(self) -> None:
        policy = SandboxPolicy(
            plugin_id="mem_hard_fail",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=2 * 1024**3),
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation, match="Hard limit violations"):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()

    def test_activate_raises_on_untrusted_threads(self) -> None:
        policy = SandboxPolicy(
            plugin_id="threads_hard_fail",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=4),
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()


class TestExecutorActivationViolation:
    async def test_executor_raises_on_activation_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="bad_exec")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            with pytest.raises(SandboxViolation):
                await executor.safe_evaluate(None, None, None)
        finally:
            executor.cleanup()

    async def test_activation_violation_records_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="metrics_violation",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        executor = PluginSandboxExecutor(
            _EmptyStrategy(), policy, metrics_collector=collector
        )
        try:
            with pytest.raises(SandboxViolation):
                await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("metrics_violation")
            assert metrics is not None
            assert metrics["total_evaluations"] == 1
            assert metrics["errors"] == 1
            assert metrics["last_error"] is not None
            assert "Trust level policy validation failed" in metrics["last_error"]
        finally:
            executor.cleanup()

    async def test_activation_violation_from_integrity_tamper_records_metrics(self) -> None:
        # from_trust_level() auto-sets _integrity_hash. We replace
        # introspection_policy and re-set the hash so tampering is
        # detected via hash mismatch rather than resource limits.
        #
        # NOTE: from_trust_level() passes the shared _TRUST_INTROSPECTION_PRESETS
        # by reference, so we replace introspection_policy with a fresh copy
        # to avoid corrupting the preset for subsequent tests.
        #
        # Tamper via blocked_builtins: in introspection_policy (different
        # sub-object from resource_policy), less likely independently validated.
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_metrics")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        policy.introspection_policy.blocked_builtins = set()
        executor = PluginSandboxExecutor(
            _EmptyStrategy(), policy, metrics_collector=collector
        )
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("tamper_metrics")
            assert metrics is not None
            assert metrics["total_evaluations"] == 1
            assert metrics["errors"] == 1
        finally:
            executor.cleanup()

    async def test_activation_violation_no_signals_returned(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="no_signals_violation")
        executor = PluginSandboxExecutor(
            _GoodStrategy(), policy, metrics_collector=collector
        )
        try:
            with pytest.raises(SandboxViolation):
                await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("no_signals_violation")
            assert metrics is not None
            assert metrics["total_signals_emitted"] == 0
        finally:
            executor.cleanup()


class TestSandboxPolicyIntegrity:
    def test_verify_integrity_detects_tampering(self) -> None:
        # from_trust_level() auto-sets _integrity_hash; calling
        # set_integrity_hash() again is idempotent and harmless.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "integ_tamper")
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True
        policy.resource_policy.max_cpu_seconds = 9999
        assert policy.verify_integrity() is False

    def test_verify_integrity_passes_without_hash(self) -> None:
        policy = SandboxPolicy(plugin_id="no_hash")
        assert policy.verify_integrity() is True

    def test_enforce_hard_limits_returns_violations(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_violations",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=200, max_memory_bytes=2 * 1024**3),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert len(violations) >= 2

    def test_enforce_hard_limits_no_violations_for_valid_policy(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "valid_hard")
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert violations == []


class TestIntegrityHashTamperDetection:
    """Comprehensive tests proving integrity-hash-based tamper detection.

    These tests break a recurring loop where tampering tests passed for the
    WRONG reason: the old tests mutated fields like max_cpu_seconds (caught
    by validate_trust_level resource thresholds) or wall_time_seconds (in
    resource_policy, which might be independently validated).

    The corrected approach:
      1. from_trust_level() now auto-sets _integrity_hash internally.
      2. Replace introspection_policy with a fresh copy to avoid mutating
         the shared _TRUST_INTROSPECTION_PRESETS (passed by reference).
      3. Tamper via blocked_builtins (introspection_policy.blocked_builtins),
         which IS included in compute_integrity_hash() (policy.py:209) but is
         NOT subject to validate_trust_level() resource checks (context.py:88-94).
         Being in a different sub-object from resource_policy makes it less
         likely to be independently validated.
      4. This guarantees the violation is detected via hash mismatch alone.
    """

    def test_from_trust_level_auto_sets_hash(self) -> None:
        # from_trust_level() now auto-sets _integrity_hash internally,
        # consistent with from_manifest() and trusted_policy().
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "auto_hash_check")
        assert policy._integrity_hash is not None

    def test_blocked_builtins_tamper_detected_via_hash(self) -> None:
        # blocked_builtins IS in compute_integrity_hash (policy.py:209).
        # Clearing it (inverted) changes the hash, so verify_integrity() fails.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "builtin_tamper")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is False

    def test_blocked_builtins_tamper_undetected_without_hash(self) -> None:
        # Without an integrity hash, verify_integrity() returns True
        # unconditionally (policy.py:219-220). This test clears the
        # auto-set hash to prove detection requires the hash to be present.
        #
        # blocked_builtins chosen: it IS in compute_integrity_hash() but
        # NOT subject to validate_trust_level() resource checks, so
        # detection is purely hash-based when the hash is present.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "no_hash_tamper")
        policy.introspection_policy = IntrospectionPolicy()
        policy._integrity_hash = None
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is True

    def test_blocked_dunder_access_not_in_hash_so_tamper_undetected(self) -> None:
        # blocked_dunder_access is NOT included in compute_integrity_hash(),
        # so mutating it does NOT change the hash. This is intentional: only
        # fields explicitly serialized into the hash (policy.py:195-212) are
        # protected. This test documents that boundary.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "dunder_tamper")
        policy.set_integrity_hash()
        policy.introspection_policy.blocked_dunder_access = False
        assert policy.verify_integrity() is True

    def test_blocked_builtins_tamper_undetected_when_hash_cleared(self) -> None:
        # Without an integrity hash, verify_integrity() returns True
        # unconditionally (policy.py:219-220). This test clears the
        # auto-set hash to prove blocked_builtins tampering goes
        # undetected when the hash is absent.
        #
        # blocked_builtins chosen over wall_time_seconds because it is
        # in a different policy sub-object (introspection_policy vs
        # resource_policy), less likely to be independently validated
        # by resource threshold checks (context.py:88-94).
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "builtin_no_hash")
        policy.introspection_policy = IntrospectionPolicy()
        policy._integrity_hash = None
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is True

    def test_blocked_builtins_tamper_detected_with_hash(self) -> None:
        # blocked_builtins IS in compute_integrity_hash (policy.py:209)
        # and is NOT checked by validate_trust_level resource thresholds
        # (context.py:88-94). With hash set, tampering IS detected.
        #
        # blocked_builtins chosen over wall_time_seconds because it is
        # in a different policy sub-object (introspection_policy vs
        # resource_policy), less likely to be independently validated.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "builtin_hash")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is False

    def test_max_cpu_tamper_detected_by_hash_but_also_by_resources(self) -> None:
        # max_cpu_seconds IS in the hash AND checked by validate_trust_level
        # (context.py:90). This test documents the ambiguity: if we tamper
        # max_cpu_seconds, we cannot know WHICH mechanism caught it. That is
        # why blocked_builtins is the preferred tampering field.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "cpu_ambiguous")
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 9999
        assert policy.verify_integrity() is False
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False
        ctx.cleanup()

    def test_blocked_builtins_tamper_detected_by_context_validate(self) -> None:
        # Full chain: from_trust_level -> set_integrity_hash -> tamper
        # blocked_builtins -> validate_trust_level returns False because
        # verify_integrity() fails. Resource checks all pass, proving
        # detection is purely hash-based.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "full_chain")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()

        # Resource checks pass before tamper
        assert len(policy.import_policy.blocked_modules) >= 10
        assert policy.resource_policy.max_cpu_seconds <= 60
        assert policy.filesystem_policy.read_write_paths == []
        assert policy.resource_policy.max_threads <= 1

        policy.introspection_policy.blocked_builtins = set()

        # Resource checks still pass after tamper (blocked_builtins not checked)
        assert len(policy.import_policy.blocked_modules) >= 10
        assert policy.resource_policy.max_cpu_seconds <= 60

        # But hash-based detection catches it
        assert policy.verify_integrity() is False
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False
        ctx.cleanup()

    async def test_blocked_builtins_tamper_activates_violation_via_hash(self) -> None:
        # End-to-end: executor activates policy, detect_tamper raises
        # SandboxViolation because verify_integrity() fails. This confirms
        # the full executor->context->policy chain detects hash-only tampering.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "e2e_hash_tamper")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        policy.introspection_policy.blocked_builtins = set()
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
                await executor.safe_evaluate(None, None, None)
        finally:
            executor.cleanup()
