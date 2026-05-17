"""Tests for the three targeted edits:

1. context.py _enforce_hard_limits: SandboxViolationCategory.RESOURCE (was INTROSPECTION)
2. context.py activate trust-level validation: SandboxViolationCategory.RESOURCE (was INTROSPECTION)
3. executor.py _evaluate_inner: self._context.cleanup() called in except SandboxViolation block
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import (
    SandboxViolation,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.executor import PluginSandboxExecutor
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.trust_levels import TrustLevel


class _StubStrategy:
    name = "stub_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


def _make_untrusted_policy(
    plugin_id: str = "test",
    *,
    blocked_count: int = 15,
    max_cpu_seconds: float = 30.0,
    max_memory_bytes: int = 512 * 1024 * 1024,
    max_threads: int = 1,
    read_write_paths: list[str] | None = None,
    block_metadata_endpoints: bool = True,
    set_integrity: bool = True,
) -> SandboxPolicy:
    policy = SandboxPolicy(
        plugin_id=plugin_id,
        trust_level="untrusted",
        import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(blocked_count)}),
        resource_policy=ResourcePolicy(
            max_cpu_seconds=max_cpu_seconds,
            max_memory_bytes=max_memory_bytes,
            max_threads=max_threads,
        ),
        filesystem_policy=FilesystemPolicy(read_write_paths=read_write_paths or []),
        network_policy=NetworkPolicy(block_metadata_endpoints=block_metadata_endpoints),
    )
    if set_integrity:
        policy.set_integrity_hash()
    return policy


# ---------------------------------------------------------------------------
# Edit 1: _enforce_hard_limits uses SandboxViolationCategory.RESOURCE
# ---------------------------------------------------------------------------


class TestEnforceHardLimitsCategory:
    def test_hard_limit_violation_category_is_resource(self) -> None:
        policy = _make_untrusted_policy(max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_hard_limit_violation_category_is_not_introspection(self) -> None:
        policy = _make_untrusted_policy(max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is not SandboxViolationCategory.INTROSPECTION

    def test_hard_limit_cpu_exceeded_category_is_resource(self) -> None:
        policy = _make_untrusted_policy(max_cpu_seconds=9999)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_hard_limit_untrusted_threads_category_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="thread_hard",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=8),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_hard_limit_multiple_violations_category_is_resource(self) -> None:
        policy = _make_untrusted_policy(
            max_cpu_seconds=9999,
            max_memory_bytes=10 * 1024**3,
            max_threads=16,
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_hard_limit_event_logged_with_resource_category(self) -> None:
        policy = _make_untrusted_policy(max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        events = context.event_logger.get_events()
        hard_limit_events = [e for e in events if "Hard limit" in e.detail]
        assert len(hard_limit_events) >= 1
        for evt in hard_limit_events:
            assert evt.category is SandboxViolationCategory.RESOURCE

    def test_hard_limit_attempted_action(self) -> None:
        policy = _make_untrusted_policy(max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.attempted_action == "trust_level_hard_limit_check"

    def test_hard_limit_plugin_id_preserved(self) -> None:
        policy = _make_untrusted_policy(plugin_id="my_plugin", max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.plugin_id == "my_plugin"


# ---------------------------------------------------------------------------
# Edit 2: activate trust-level validation uses SandboxViolationCategory.RESOURCE
# ---------------------------------------------------------------------------


class TestTrustLevelValidationCategory:
    def test_empty_blocked_modules_category_is_resource(self) -> None:
        policy = SandboxPolicy(plugin_id="empty_blocked")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_empty_blocked_modules_category_is_not_introspection(self) -> None:
        policy = SandboxPolicy(plugin_id="not_introspect")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is not SandboxViolationCategory.INTROSPECTION

    def test_cpu_exceeds_validate_limit_category_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="cpu_validate",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=90),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_rw_paths_untrusted_category_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="rw_paths",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data/write"]),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_threads_untrusted_category_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="threads_validate",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=4),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_tampered_integrity_category_is_resource(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_cat")
        policy.resource_policy.max_cpu_seconds = 9999
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_trust_level_validation_event_logged_with_resource(self) -> None:
        policy = SandboxPolicy(plugin_id="log_event_cat")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        events = context.event_logger.get_events()
        trust_events = [e for e in events if "Trust level policy validation failed" in e.detail]
        assert len(trust_events) >= 1
        for evt in trust_events:
            assert evt.category is SandboxViolationCategory.RESOURCE

    def test_trust_level_validation_attempted_action(self) -> None:
        policy = SandboxPolicy(plugin_id="action_check")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.attempted_action == "trust_level_validation"

    def test_trust_level_validation_plugin_id(self) -> None:
        policy = SandboxPolicy(plugin_id="pid_check")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.plugin_id == "pid_check"

    def test_trust_level_validation_detail_contains_trust_level(self) -> None:
        policy = SandboxPolicy(plugin_id="detail_check", trust_level="untrusted")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert "untrusted" in exc_info.value.detail

    def test_trusted_limited_insufficient_blocked_category_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_fail",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE


# ---------------------------------------------------------------------------
# Edit 3: executor calls self._context.cleanup() on activation violation
# ---------------------------------------------------------------------------


class TestExecutorCleanupOnActivationViolation:
    async def test_cleanup_called_on_activation_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="cleanup_test")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)

        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock_cleanup:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            mock_cleanup.assert_called_once()

    async def test_cleanup_called_before_return(self) -> None:
        policy = SandboxPolicy(plugin_id="cleanup_order")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)

        call_order = []
        original_cleanup = executor._context.cleanup

        def tracking_cleanup():
            call_order.append("cleanup")
            return original_cleanup()

        with patch.object(executor._context, "cleanup", side_effect=tracking_cleanup):
            signals = await executor.safe_evaluate(None, None, None)
            call_order.append("return")
            assert signals == []

        assert call_order.index("cleanup") < call_order.index("return")

    async def test_no_cleanup_on_successful_activation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "success_no_cleanup")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)

        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock_cleanup:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            mock_cleanup.assert_not_called()
        executor.cleanup()

    async def test_metrics_still_recorded_on_violation(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="metrics_viol")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)

        signals = await executor.safe_evaluate(None, None, None)
        assert signals == []

        metrics = collector.get_plugin_metrics("metrics_viol")
        assert metrics is not None
        assert metrics["errors"] == 1

    async def test_returns_empty_list_on_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="empty_ret")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            assert isinstance(signals, list)
        finally:
            executor.cleanup()

    async def test_context_not_active_after_violation_cleanup(self) -> None:
        policy = SandboxPolicy(plugin_id="not_active")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)

        await executor.safe_evaluate(None, None, None)
        assert executor._context.is_active is False

    async def test_multiple_violation_calls_each_trigger_cleanup(self) -> None:
        policy = SandboxPolicy(plugin_id="multi_viol")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)

        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock_cleanup:
            await executor.safe_evaluate(None, None, None)
            await executor.safe_evaluate(None, None, None)
            await executor.safe_evaluate(None, None, None)
            assert mock_cleanup.call_count == 3


# ---------------------------------------------------------------------------
# Integration: combined category + cleanup behavior
# ---------------------------------------------------------------------------


class TestCategoryAndCleanupIntegration:
    def test_hard_limit_violation_event_category_matches_exception(self) -> None:
        policy = _make_untrusted_policy(max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()

        events = context.event_logger.get_events(category=SandboxViolationCategory.RESOURCE)
        assert len(events) >= 1
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_trust_validation_violation_event_category_matches_exception(self) -> None:
        policy = SandboxPolicy(plugin_id="integ_trust")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()

        events = context.event_logger.get_events(category=SandboxViolationCategory.RESOURCE)
        assert len(events) >= 1
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_no_introspection_events_from_context_violations(self) -> None:
        policy = _make_untrusted_policy(max_memory_bytes=2 * 1024**3)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()

        introspection_events = context.event_logger.get_events(
            category=SandboxViolationCategory.INTROSPECTION,
        )
        assert len(introspection_events) == 0

    def test_no_introspection_events_from_trust_validation(self) -> None:
        policy = SandboxPolicy(plugin_id="no_introspect")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()

        introspection_events = context.event_logger.get_events(
            category=SandboxViolationCategory.INTROSPECTION,
        )
        assert len(introspection_events) == 0

    async def test_executor_violation_records_resource_category(self) -> None:
        policy = SandboxPolicy(plugin_id="executor_cat")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)

        await executor.safe_evaluate(None, None, None)

        events = executor._context.event_logger.get_events(category=SandboxViolationCategory.RESOURCE)
        assert len(events) >= 1

    async def test_full_pipeline_valid_policy_no_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "full_ok")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            events = executor._context.event_logger.get_events()
            assert len(events) == 0
        finally:
            executor.cleanup()


# ---------------------------------------------------------------------------
# Edge cases and boundary values
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_hard_limit_memory_exactly_at_boundary_no_violation(self) -> None:
        policy = _make_untrusted_policy(
            max_memory_bytes=1024**3,
            max_cpu_seconds=30.0,
        )
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("Should not raise at exact memory hard limit boundary")
        finally:
            context.cleanup()

    def test_hard_limit_cpu_exactly_at_validate_boundary_no_violation(self) -> None:
        policy = _make_untrusted_policy(max_cpu_seconds=60.0)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("Should not raise at exact CPU validate boundary")
        finally:
            context.cleanup()

    def test_hard_limit_just_over_boundary_raises(self) -> None:
        policy = _make_untrusted_policy(
            max_memory_bytes=1024**3 + 1,
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_cpu_validate_just_over_boundary(self) -> None:
        policy = _make_untrusted_policy(
            max_cpu_seconds=60.01,
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_cpu_validate_exactly_at_boundary(self) -> None:
        policy = _make_untrusted_policy(
            max_cpu_seconds=60.0,
        )
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("Should not raise at exact validate boundary")
        finally:
            context.cleanup()

    def test_blocked_modules_exactly_minimum(self) -> None:
        policy = _make_untrusted_policy(blocked_count=10)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("Should not raise at minimum blocked modules")
        finally:
            context.cleanup()

    def test_blocked_modules_just_below_minimum(self) -> None:
        policy = _make_untrusted_policy(blocked_count=9)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_threads_exactly_one_no_violation(self) -> None:
        policy = _make_untrusted_policy(max_threads=1)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("Should not raise with exactly 1 thread")
        finally:
            context.cleanup()

    def test_threads_two_raises(self) -> None:
        policy = _make_untrusted_policy(max_threads=2)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_context_activate_idempotent_when_active(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "idempotent")
        context = SandboxContext(policy)
        try:
            context.activate()
            assert context.is_active is True
            context.activate()
            assert context.is_active is True
        finally:
            context.cleanup()

    def test_context_deactivate_when_inactive_is_noop(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "deactivate_noop")
        context = SandboxContext(policy)
        context.deactivate()
        assert context.is_active is False

    def test_invalid_trust_level_defaults_to_untrusted(self) -> None:
        policy = SandboxPolicy(plugin_id="bad_trust_val", trust_level="nonexistent")
        context = SandboxContext(policy)
        assert context.trust_level == TrustLevel.UNTRUSTED

    async def test_executor_with_factory_cleanup(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "factory_clean")

        def factory():
            return _StubStrategy()

        executor = PluginSandboxExecutor.from_factory(factory, policy)
        assert executor.strategy.name == "stub_strategy"
        executor.cleanup()
