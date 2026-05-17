"""Tests for POLICY violation category and executor cleanup on activation violation.

Covers:
1. SandboxViolationCategory.POLICY exists and has correct value
2. SandboxContext uses POLICY category for hard-limit violations (not INTROSPECTION)
3. SandboxContext uses POLICY category for trust-level validation failures
4. PluginSandboxExecutor calls self._context.cleanup() on activation violation
5. Edge cases: violation category is inspectable, to_dict works, multiple violations
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.core.signal import Signal
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
        return [Signal.buy(symbol="TSLA", strategy_id=self.name)]


def _make_untrusted_policy_with_violation(
    plugin_id: str = "test_policy",
    *,
    max_cpu_seconds: float = 30.0,
    max_memory_bytes: int = 512 * 1024**2,
    read_write_paths: list[str] | None = None,
    max_threads: int = 1,
    block_metadata_endpoints: bool = True,
) -> SandboxPolicy:
    blocked = {f"_blocked_module_{i}" for i in range(15)}
    return SandboxPolicy(
        plugin_id=plugin_id,
        trust_level="untrusted",
        import_policy=ImportPolicy(blocked_modules=blocked),
        resource_policy=ResourcePolicy(
            max_cpu_seconds=max_cpu_seconds,
            max_memory_bytes=max_memory_bytes,
            max_threads=max_threads,
        ),
        filesystem_policy=FilesystemPolicy(
            read_write_paths=read_write_paths or [],
        ),
        network_policy=NetworkPolicy(
            block_metadata_endpoints=block_metadata_endpoints,
        ),
    )


class TestPolicyCategoryEnum:
    def test_policy_category_exists(self) -> None:
        assert hasattr(SandboxViolationCategory, "POLICY")

    def test_policy_category_value(self) -> None:
        assert SandboxViolationCategory.POLICY.value == "policy"

    def test_policy_is_distinct_from_introspection(self) -> None:
        assert SandboxViolationCategory.POLICY is not SandboxViolationCategory.INTROSPECTION

    def test_policy_category_in_all_members(self) -> None:
        members = list(SandboxViolationCategory)
        assert SandboxViolationCategory.POLICY in members

    def test_total_category_count(self) -> None:
        assert len(SandboxViolationCategory) == 6


class TestPolicyCategoryOnHardLimitViolation:
    def test_hard_limit_cpu_uses_policy_category(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "cpu_policy", max_cpu_seconds=200,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_hard_limit_memory_uses_policy_category(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "mem_policy", max_memory_bytes=5 * 1024**3,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_hard_limit_threads_uses_policy_category_via_validate(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "threads_policy", max_threads=8,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_hard_limit_rw_paths_uses_policy_category_via_validate(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "rw_policy", read_write_paths=["/data/write"],
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_hard_limit_violation_attempted_action(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "action_test", max_memory_bytes=5 * 1024**3,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.attempted_action == "trust_level_hard_limit_check"
        finally:
            context.cleanup()

    def test_hard_limit_violation_plugin_id(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "plugin_id_test", max_cpu_seconds=200,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.plugin_id == "plugin_id_test"
        finally:
            context.cleanup()

    def test_hard_limit_violation_message_content(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "msg_test", max_memory_bytes=5 * 1024**3,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert "Hard limit violations" in str(exc_info.value)
        finally:
            context.cleanup()

    def test_hard_limit_violation_to_dict_category(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "todict_test", max_cpu_seconds=200,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            d = exc_info.value.to_dict()
            assert d["category"] == "policy"
        finally:
            context.cleanup()

    def test_metadata_endpoint_violation_uses_policy_category(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "meta_test", block_metadata_endpoints=False,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()


class TestPolicyCategoryOnTrustValidationFailure:
    def test_empty_blocked_modules_uses_policy_category(self) -> None:
        policy = SandboxPolicy(plugin_id="empty_blocks")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_cpu_exceeds_validate_limit_uses_policy(self) -> None:
        policy = SandboxPolicy(
            plugin_id="cpu_validate",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=90),
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_tampered_integrity_uses_policy_category(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper")
        policy.resource_policy.max_cpu_seconds = 9999
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
        finally:
            context.cleanup()

    def test_trust_validation_attempted_action(self) -> None:
        policy = SandboxPolicy(plugin_id="action_trust")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.attempted_action == "trust_level_validation"
        finally:
            context.cleanup()

    def test_trust_validation_plugin_id(self) -> None:
        policy = SandboxPolicy(plugin_id="my_plugin_123")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.plugin_id == "my_plugin_123"
        finally:
            context.cleanup()

    def test_trust_validation_message(self) -> None:
        policy = SandboxPolicy(plugin_id="msg_trust", trust_level="untrusted")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert "Trust level policy validation failed" in str(exc_info.value)
            assert "untrusted" in str(exc_info.value)
        finally:
            context.cleanup()

    def test_trust_validation_to_dict(self) -> None:
        policy = SandboxPolicy(plugin_id="dict_trust")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            d = exc_info.value.to_dict()
            assert d["category"] == "policy"
            assert d["attempted_action"] == "trust_level_validation"
            assert d["plugin_id"] == "dict_trust"
        finally:
            context.cleanup()


class TestNoIntrospectionCategoryMisuse:
    def test_hard_limit_does_not_use_introspection(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "no_intro", max_cpu_seconds=999,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is not SandboxViolationCategory.INTROSPECTION
        finally:
            context.cleanup()

    def test_trust_validation_does_not_use_introspection(self) -> None:
        policy = SandboxPolicy(plugin_id="no_intro_trust")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is not SandboxViolationCategory.INTROSPECTION
        finally:
            context.cleanup()


class TestExecutorCleanupOnActivationViolation:
    async def test_cleanup_called_on_activation_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="cleanup_test")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock_cleanup:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            mock_cleanup.assert_called_once()

    async def test_cleanup_records_metrics_on_activation_violation(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="cleanup_metrics")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
            metrics = collector.get_plugin_metrics("cleanup_metrics")
            assert metrics is not None
            assert metrics["errors"] == 1
        finally:
            executor.cleanup()

    async def test_context_not_active_after_activation_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="inactive_test")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        try:
            await executor.safe_evaluate(None, None, None)
            assert executor._context.is_active is False
        finally:
            executor.cleanup()

    async def test_executor_can_be_reused_after_activation_violation_cleanup(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="reuse_test")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)
        try:
            signals1 = await executor.safe_evaluate(None, None, None)
            assert signals1 == []
            signals2 = await executor.safe_evaluate(None, None, None)
            assert signals2 == []
            metrics = collector.get_plugin_metrics("reuse_test")
            assert metrics["errors"] == 2
        finally:
            executor.cleanup()

    async def test_executor_explicit_cleanup_after_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="explicit_cleanup")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        signals = await executor.safe_evaluate(None, None, None)
        assert signals == []
        executor.cleanup()
        assert executor._context.is_active is False

    async def test_multiple_safe_evaluates_with_bad_policy(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="multi_fail")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)
        try:
            for _ in range(5):
                signals = await executor.safe_evaluate(None, None, None)
                assert signals == []
            metrics = collector.get_plugin_metrics("multi_fail")
            assert metrics["total_evaluations"] == 5
            assert metrics["errors"] == 5
        finally:
            executor.cleanup()


class TestExecutorCleanupWithValidPolicy:
    async def test_no_cleanup_on_successful_activation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "success_no_cleanup")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "cleanup") as mock_cleanup:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            mock_cleanup.assert_not_called()
        executor.cleanup()

    async def test_deactivate_called_on_successful_path(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "deactivate_test")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "deactivate", wraps=executor._context.deactivate) as mock_deactivate:
            await executor.safe_evaluate(None, None, None)
            mock_deactivate.assert_called()
        executor.cleanup()


class TestViolationCategoryEventLog:
    def test_hard_limit_event_logged_with_policy_category(self) -> None:
        policy = _make_untrusted_policy_with_violation(
            "event_log_hard", max_memory_bytes=5 * 1024**3,
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                context.activate()
            events = context.event_logger.get_events()
            assert any(
                e.category == SandboxViolationCategory.POLICY
                and "Hard limit" in e.detail
                for e in events
            )
        finally:
            context.cleanup()

    def test_trust_validation_event_logged_with_policy_category(self) -> None:
        policy = SandboxPolicy(plugin_id="event_log_trust")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                context.activate()
            events = context.event_logger.get_events()
            assert any(
                e.category == SandboxViolationCategory.POLICY
                and "Trust level policy validation failed" in e.detail
                for e in events
            )
        finally:
            context.cleanup()

    def test_no_introspection_events_for_policy_violations(self) -> None:
        policy = SandboxPolicy(plugin_id="no_intro_events")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                context.activate()
            events = context.event_logger.get_events()
            introspection_events = [
                e for e in events if e.category == SandboxViolationCategory.INTROSPECTION
            ]
            assert len(introspection_events) == 0
        finally:
            context.cleanup()


class TestViolationCategoryBoundaryCases:
    def test_policy_violation_is_catchable_as_sandbox_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="catchable")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                context.activate()
        finally:
            context.cleanup()

    def test_policy_violation_is_catchable_as_exception(self) -> None:
        policy = SandboxPolicy(plugin_id="exception_base")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                context.activate()
        finally:
            context.cleanup()

    def test_context_stays_inactive_after_policy_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="inactive")
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                context.activate()
            assert context.is_active is False
        finally:
            context.cleanup()

    def test_context_can_be_cleaned_up_after_policy_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="cleanable")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        context.cleanup()
        assert context.is_active is False

    def test_double_cleanup_after_violation_is_safe(self) -> None:
        policy = SandboxPolicy(plugin_id="double_clean")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        context.cleanup()
        context.cleanup()
        assert context.is_active is False

    def test_combined_hard_limit_and_validate_violation(self) -> None:
        policy = SandboxPolicy(
            plugin_id="combined",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(
                max_cpu_seconds=999,
                max_memory_bytes=10 * 1024**3,
                max_threads=8,
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert len(violations) >= 3

    def test_trusted_full_cpu_exceeds_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="full_cpu_hard",
            trust_level="trusted_full",
            resource_policy=ResourcePolicy(max_cpu_seconds=700),
        )
        context = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation) as exc_info:
                context.activate()
            assert exc_info.value.category is SandboxViolationCategory.POLICY
            assert "Hard limit" in str(exc_info.value)
        finally:
            context.cleanup()
