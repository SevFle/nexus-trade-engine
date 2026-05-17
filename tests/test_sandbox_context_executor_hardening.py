"""Comprehensive tests for sandbox context, executor, and trust enforcement hardening.

Covers:
- Context lifecycle (activate/deactivate idempotency, context manager)
- Trust validation (validate_trust_level boundary conditions)
- Integrity tamper detection (verify_integrity as the actual failure point)
- Hard limits enforcement (_enforce_hard_limits without duplicate logging)
- Violation collection and metrics propagation
- Executor activation violation propagation with metrics recording
- Untrusted policy with insufficient blocked_modules triggering SandboxViolation
"""

from __future__ import annotations

import pytest

from engine.core.signal import Signal
from engine.plugins.sandbox.core.context import (
    _MAX_CPU_SECONDS_LIMITED,
    _MAX_CPU_SECONDS_UNTRUSTED,
    _MIN_BLOCKED_MODULES_LIMITED,
    _MIN_BLOCKED_MODULES_UNTRUSTED,
    SandboxContext,
)
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    NetworkPolicy,
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


class _EmptyStrategy:
    name = "empty_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class _AsyncStrategy:
    name = "async_strategy"
    version = "1.0.0"

    async def on_bar(self, state, portfolio):
        return []


def _make_untrusted_policy_with_blocked(count: int, **kwargs):
    blocked = {f"mod_{i}" for i in range(count)}
    return SandboxPolicy(
        plugin_id=kwargs.get("plugin_id", "test"),
        trust_level="untrusted",
        import_policy=ImportPolicy(blocked_modules=blocked),
        resource_policy=ResourcePolicy(max_cpu_seconds=30),
        filesystem_policy=FilesystemPolicy(),
        network_policy=NetworkPolicy(),
    )


def _make_limited_policy_with_blocked(count: int, **kwargs):
    blocked = {f"mod_{i}" for i in range(count)}
    return SandboxPolicy(
        plugin_id=kwargs.get("plugin_id", "test_limited"),
        trust_level="trusted_limited",
        import_policy=ImportPolicy(blocked_modules=blocked),
        resource_policy=ResourcePolicy(max_cpu_seconds=60),
    )


class TestInsufficientBlockedModulesActivationViolation:
    def test_untrusted_insufficient_blocked_modules_raises_in_activate(self) -> None:
        policy = _make_untrusted_policy_with_blocked(1, plugin_id="insufficient_mods")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()
        assert context.is_active is False

    def test_untrusted_insufficient_blocked_modules_propagates_via_executor(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(1, plugin_id="exec_insufficient")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            executor._context.activate()

    @pytest.mark.asyncio
    async def test_untrusted_insufficient_blocked_modules_records_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(1, plugin_id="metrics_insufficient")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation):
            await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("metrics_insufficient")
        assert metrics is not None
        assert metrics["total_evaluations"] == 1
        assert metrics["errors"] == 1
        assert metrics["total_signals_emitted"] == 0
        assert "Trust level policy validation failed" in metrics["last_error"]

    def test_limited_insufficient_blocked_modules_raises(self) -> None:
        policy = _make_limited_policy_with_blocked(1, plugin_id="limited_insufficient")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()
        assert context.is_active is False

    @pytest.mark.asyncio
    async def test_limited_insufficient_blocked_modules_executor_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_limited_policy_with_blocked(1, plugin_id="limited_exec_metrics")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation):
            await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("limited_exec_metrics")
        assert metrics is not None
        assert metrics["errors"] == 1


class TestContextLifecycle:
    def test_activate_sets_active_flag(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="lifecycle_test")
        context = SandboxContext(policy)
        assert context.is_active is False
        context.activate()
        assert context.is_active is True
        context.deactivate()

    def test_activate_idempotent(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="idempotent_test")
        context = SandboxContext(policy)
        context.activate()
        assert context.is_active is True
        context.activate()
        assert context.is_active is True
        context.deactivate()

    def test_deactivate_when_not_active(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="deact_inactive")
        context = SandboxContext(policy)
        assert context.is_active is False
        context.deactivate()
        assert context.is_active is False

    def test_deactivate_sets_inactive(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="deact_test")
        context = SandboxContext(policy)
        context.activate()
        assert context.is_active is True
        context.deactivate()
        assert context.is_active is False

    def test_context_manager_protocol(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="ctx_mgr")
        context = SandboxContext(policy)
        with context:
            assert context.is_active is True
        assert context.is_active is False

    def test_context_manager_deactivates_on_exception(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "ctx_mgr_exc")
        context = SandboxContext(policy)

        def _raise_inside_ctx():
            with context:
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            _raise_inside_ctx()
        assert context.is_active is False

    def test_cleanup_deactivates_and_cleans(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="cleanup_test")
        context = SandboxContext(policy)
        context.activate()
        assert context.is_active is True
        context.cleanup()
        assert context.is_active is False

    def test_cleanup_when_not_active(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="cleanup_inactive")
        context = SandboxContext(policy)
        assert context.is_active is False
        context.cleanup()
        assert context.is_active is False


class TestContextTrustValidationEdgeCases:
    def test_untrusted_exactly_at_min_blocked_modules_passes(self) -> None:
        policy = _make_untrusted_policy_with_blocked(
            _MIN_BLOCKED_MODULES_UNTRUSTED, plugin_id="exact_min"
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_untrusted_one_below_min_blocked_modules_fails(self) -> None:
        policy = _make_untrusted_policy_with_blocked(
            _MIN_BLOCKED_MODULES_UNTRUSTED - 1, plugin_id="one_below_min"
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_untrusted_cpu_at_limit_passes(self) -> None:
        policy = SandboxPolicy(
            plugin_id="cpu_at_limit",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_untrusted_cpu_one_above_limit_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="cpu_over_limit",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED + 1),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_limited_exactly_at_min_blocked_modules_passes(self) -> None:
        policy = _make_limited_policy_with_blocked(
            _MIN_BLOCKED_MODULES_LIMITED, plugin_id="limited_exact_min"
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_limited_cpu_at_limit_passes(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_cpu_at",
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_limited_cpu_above_limit_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_cpu_over",
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED + 1),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_trusted_full_with_minimal_policy_passes(self) -> None:
        policy = SandboxPolicy(
            plugin_id="trusted_minimal",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_invalid_trust_level_resolves_to_untrusted(self) -> None:
        policy = SandboxPolicy(
            plugin_id="invalid_trust",
            trust_level="nonexistent",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        context = SandboxContext(policy)
        assert context.trust_level == TrustLevel.UNTRUSTED
        assert context.validate_trust_level() is True

    def test_untrusted_max_threads_exactly_1_passes(self) -> None:
        policy = SandboxPolicy(
            plugin_id="threads_1",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=1),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_untrusted_max_threads_2_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="threads_2",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=2),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False


class TestIntegrityTamperDetection:
    def test_tampered_allowed_endpoints_triggers_verify_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_endpoints")
        assert policy.verify_integrity() is True
        policy.network_policy.allowed_endpoints = ["tampered.evil.com"]
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_tampered_max_file_descriptors_triggers_verify_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_fd")
        assert policy.verify_integrity() is True
        policy.resource_policy.max_file_descriptors = 9999
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_tampered_read_only_paths_triggers_verify_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_ro")
        assert policy.verify_integrity() is True
        policy.filesystem_policy.read_only_paths = ["/etc/shadow"]
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_tampered_plugin_id_triggers_verify_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "original_id")
        assert policy.verify_integrity() is True
        policy.plugin_id = "tampered_id"
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_tampered_blocked_modules_set_triggers_verify_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_imports")
        assert policy.verify_integrity() is True
        policy.import_policy.blocked_modules = set()
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_tampered_max_cpu_seconds_triggers_verify_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "tamper_cpu_trusted")
        assert policy.verify_integrity() is True
        policy.resource_policy.max_cpu_seconds = 99999
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_no_integrity_hash_passes(self) -> None:
        policy = SandboxPolicy(
            plugin_id="no_hash",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_tampered_memory_bytes_caught_by_verify_integrity_only(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_rw")
        assert policy.verify_integrity() is True
        policy.resource_policy.max_memory_bytes = 999999
        assert policy.verify_integrity() is False
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()


class TestHardLimitsEnforcement:
    def test_trusted_full_cpu_exceeds_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_cpu_full",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules={"subprocess"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=700),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Hard limit violations"):
            context.activate()

    def test_untrusted_memory_exceeds_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_mem",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=2 * 1024**3),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Hard limit violations"):
            context.activate()

    def test_untrusted_rw_paths_caught_by_validate_trust_level(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_rw",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data"]),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_untrusted_threads_caught_by_validate_trust_level(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_threads",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=4),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            context.activate()

    def test_untrusted_metadata_endpoints_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_metadata",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Hard limit violations"):
            context.activate()

    def test_multiple_hard_limit_violations(self) -> None:
        policy = SandboxPolicy(
            plugin_id="multi_hard",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=2 * 1024**3),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Hard limit violations") as exc_info:
            context.activate()
        msg = str(exc_info.value)
        assert "max_memory_bytes" in msg
        assert "metadata" in msg

    def test_hard_limit_does_not_log_to_event_logger(self) -> None:
        policy = SandboxPolicy(
            plugin_id="no_log_hard",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=2 * 1024**3),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        assert context.event_logger.event_count == 0


class TestViolationCollectionAndMetrics:
    def test_collect_violations_without_metrics_collector(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="no_metrics")
        context = SandboxContext(policy)
        context.activate()
        context.deactivate()

    def test_collect_violations_with_metrics_collector(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="with_metrics")
        context = SandboxContext(policy, metrics_collector=collector)
        context.activate()
        context.deactivate()

    def test_event_logger_records_no_events_on_clean_run(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="clean_run")
        context = SandboxContext(policy)
        context.activate()
        context.deactivate()
        assert context.event_logger.event_count == 0

    def test_event_logger_plugin_id_matches_policy(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="logger_id_test")
        context = SandboxContext(policy)
        context.activate()
        context.deactivate()
        events = context.event_logger.get_events()
        assert all(e.plugin_id == "logger_id_test" for e in events)


class TestExecutorActivationViolationPropagation:
    def test_activation_violation_propagates_exception(self) -> None:
        policy = _make_untrusted_policy_with_blocked(1, plugin_id="propagate_test")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        with pytest.raises(SandboxViolation):
            executor._context.activate()

    @pytest.mark.asyncio
    async def test_activation_violation_records_evaluation_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="eval_metrics",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation):
            await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("eval_metrics")
        assert metrics is not None
        assert metrics["total_evaluations"] == 1
        assert metrics["errors"] == 1
        assert "Hard limit violations" in metrics["last_error"]

    @pytest.mark.asyncio
    async def test_activation_violation_zero_signals(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(1, plugin_id="zero_sig")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation):
            await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("zero_sig")
        assert metrics is not None
        assert metrics["total_signals_emitted"] == 0

    @pytest.mark.asyncio
    async def test_hard_limit_violation_propagates_via_executor(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(
            plugin_id="exec_hard_limit",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=2 * 1024**3),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation, match="Hard limit violations"):
            await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("exec_hard_limit")
        assert metrics is not None
        assert metrics["errors"] == 1

    @pytest.mark.asyncio
    async def test_integrity_tamper_propagates_via_executor(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "exec_tamper")
        policy.network_policy.allowed_endpoints = ["tampered.evil.com"]
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("exec_tamper")
        assert metrics is not None
        assert metrics["errors"] == 1
        assert "Trust level policy validation failed" in metrics["last_error"]


class TestExecutorEvaluationScenarios:
    @pytest.mark.asyncio
    async def test_good_strategy_returns_signals(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="good_eval")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
            assert signals[0].strategy_id == "good_strategy"
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_empty_strategy_returns_empty(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="empty_eval")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_async_strategy_handled(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="async_eval")
        executor = PluginSandboxExecutor(_AsyncStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_signal_strategy_id_injected(self) -> None:
        class _NoIdStrategy:
            name = "noid_strategy"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                return [Signal.buy(symbol="TSLA", strategy_id="")]

        policy = _make_untrusted_policy_with_blocked(15, plugin_id="id_inject")
        executor = PluginSandboxExecutor(_NoIdStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].strategy_id == "noid_strategy"
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_invalid_signals_filtered(self) -> None:
        class _MixedStrategy:
            name = "mixed_strategy"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                return [
                    Signal.buy(symbol="AAPL", strategy_id="mixed_strategy"),
                    "invalid",
                    42,
                ]

        policy = _make_untrusted_policy_with_blocked(15, plugin_id="mixed_eval")
        executor = PluginSandboxExecutor(_MixedStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_metrics_recorded_on_success(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="success_metrics")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        try:
            await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("success_metrics")
            assert metrics is not None
            assert metrics["total_evaluations"] == 1
            assert metrics["total_signals_emitted"] == 1
            assert metrics["total_cpu_time_ms"] > 0
            assert metrics["errors"] == 0
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_metrics_accumulate_over_evaluations(self) -> None:
        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="accum")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy, metrics_collector=collector)
        try:
            for _ in range(5):
                await executor.safe_evaluate(None, None, None)
            metrics = collector.get_plugin_metrics("accum")
            assert metrics is not None
            assert metrics["total_evaluations"] == 5
        finally:
            executor.cleanup()

    @pytest.mark.asyncio
    async def test_strategy_exception_returns_empty_and_records_error(self) -> None:
        class _CrashStrategy:
            name = "crash_strategy"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                raise RuntimeError("kaboom")

        collector = SandboxMetricsCollector()
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="crash_eval")
        executor = PluginSandboxExecutor(_CrashStrategy(), policy, metrics_collector=collector)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            metrics = collector.get_plugin_metrics("crash_eval")
            assert metrics is not None
            assert metrics["errors"] == 1
        finally:
            executor.cleanup()


class TestExecutorHealthAndFactory:
    @pytest.mark.asyncio
    async def test_health_report(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="health_rpt")
        executor = PluginSandboxExecutor(_GoodStrategy(), policy)
        try:
            await executor.safe_evaluate(None, None, None)
            health = executor.get_health()
            assert health["strategy_name"] == "good_strategy"
            assert health["version"] == "1.0.0"
            assert health["plugin_id"] == "health_rpt"
            assert health["trust_level"] == "untrusted"
            assert health["total_evaluations"] == 1
        finally:
            executor.cleanup()

    def test_health_report_before_evaluation(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="health_pre")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        health = executor.get_health()
        assert health["strategy_name"] == "empty_strategy"

    def test_from_factory_creates_executor(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="factory")

        def factory():
            return _EmptyStrategy()

        executor = PluginSandboxExecutor.from_factory(factory, policy)
        assert executor.strategy.name == "empty_strategy"

    def test_cleanup_after_evaluation(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="cleanup_exec")
        executor = PluginSandboxExecutor(_EmptyStrategy(), policy)
        executor.cleanup()
        assert executor._context.is_active is False


class TestTrustLevelConstants:
    def test_min_blocked_modules_untrusted_is_10(self) -> None:
        assert _MIN_BLOCKED_MODULES_UNTRUSTED == 10

    def test_min_blocked_modules_limited_is_5(self) -> None:
        assert _MIN_BLOCKED_MODULES_LIMITED == 5

    def test_max_cpu_seconds_untrusted_is_60(self) -> None:
        assert _MAX_CPU_SECONDS_UNTRUSTED == 60

    def test_max_cpu_seconds_limited_is_120(self) -> None:
        assert _MAX_CPU_SECONDS_LIMITED == 120


class TestContextProperties:
    def test_policy_property(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="prop_test")
        context = SandboxContext(policy)
        assert context.policy is policy

    def test_trust_level_property(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="trust_prop")
        context = SandboxContext(policy)
        assert context.trust_level == TrustLevel.UNTRUSTED

    def test_trust_level_property_limited(self) -> None:
        policy = _make_limited_policy_with_blocked(10, plugin_id="trust_lim")
        context = SandboxContext(policy)
        assert context.trust_level == TrustLevel.TRUSTED_LIMITED

    def test_trust_level_property_full(self) -> None:
        policy = SandboxPolicy(
            plugin_id="trust_full",
            trust_level="trusted_full",
        )
        context = SandboxContext(policy)
        assert context.trust_level == TrustLevel.TRUSTED_FULL

    def test_work_dir_property(self) -> None:
        policy = _make_untrusted_policy_with_blocked(15, plugin_id="workdir")
        context = SandboxContext(policy)
        assert context.work_dir


class TestValidateTrustLevelIsolated:
    def test_untrusted_all_conditions_pass(self) -> None:
        policy = _make_untrusted_policy_with_blocked(
            _MIN_BLOCKED_MODULES_UNTRUSTED, plugin_id="vtl_pass"
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_untrusted_empty_blocked_modules_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="vtl_empty",
            trust_level="untrusted",
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_untrusted_cpu_exceeds_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="vtl_cpu",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=90),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_untrusted_rw_paths_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="vtl_rw",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/var/data/workspace"]),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_untrusted_threads_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="vtl_threads",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=2),
            import_policy=ImportPolicy(
                blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
            ),
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_limited_all_conditions_pass(self) -> None:
        policy = _make_limited_policy_with_blocked(
            _MIN_BLOCKED_MODULES_LIMITED, plugin_id="vtl_lim_pass"
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is True

    def test_limited_empty_blocked_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="vtl_lim_empty",
            trust_level="trusted_limited",
        )
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False

    def test_tampered_integrity_causes_failure(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "vtl_tamper")
        policy.plugin_id = "tampered"
        context = SandboxContext(policy)
        assert context.validate_trust_level() is False
