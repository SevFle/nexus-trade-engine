"""Comprehensive tests for the three targeted edits to break the loop:

Edit 1: context.py _enforce_hard_limits — INTROSPECTION → RESOURCE
Edit 2: context.py activate trust validation — INTROSPECTION → RESOURCE (policy category)
Edit 3: executor.py _evaluate_inner — self._context.cleanup() in except SandboxViolation

These tests verify correctness, integration, edge cases, and regression safety.
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
    name = "stub"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class _SignalStrategy:
    name = "signal_gen"
    version = "2.0.0"

    def on_bar(self, state, portfolio):
        from engine.core.signal import Signal
        return [Signal.buy(symbol="TSLA", strategy_id=self.name)]


def _valid_untrusted_policy(
    plugin_id: str = "test",
    *,
    blocked_count: int = 15,
    max_cpu_seconds: float = 30.0,
    max_memory_bytes: int = 512 * 1024 * 1024,
    max_threads: int = 1,
    read_write_paths: list[str] | None = None,
    block_metadata_endpoints: bool = True,
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
    policy.set_integrity_hash()
    return policy


def _invalid_untrusted_policy_violates_hard(**kwargs) -> SandboxPolicy:
    kwargs.setdefault("max_memory_bytes", 2 * 1024**3)
    return _valid_untrusted_policy(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# EDIT 1: _enforce_hard_limits category correctness
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnforceHardLimitsResourceCategory:
    def test_cpu_hard_limit_violation_is_resource_not_introspection(self) -> None:
        policy = _valid_untrusted_policy(max_cpu_seconds=9999)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE
        assert exc_info.value.category is not SandboxViolationCategory.INTROSPECTION

    def test_memory_hard_limit_violation_is_resource(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_threads_hard_limit_violation_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="thread_test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=8),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_untrusted_rw_paths_hard_limit_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="rw_hard",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_metadata_endpoints_hard_limit_is_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="meta_ep",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_combined_hard_limit_violations_single_resource_category(self) -> None:
        policy = SandboxPolicy(
            plugin_id="combined_hard",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=10 * 1024**3, max_threads=1),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE
        assert "Hard limit" in exc_info.value.detail
        assert "max_memory_bytes" in exc_info.value.detail

    def test_event_logged_before_exception_raised(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="event_order")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        events = context.event_logger.get_events()
        hard_events = [e for e in events if "Hard limit" in e.detail]
        assert len(hard_events) >= 1
        assert hard_events[0].category is SandboxViolationCategory.RESOURCE

    def test_hard_limit_event_has_correct_plugin_id(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="hard_pid")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        events = context.event_logger.get_events()
        hard_events = [e for e in events if "Hard limit" in e.detail]
        assert all(e.plugin_id == "hard_pid" for e in hard_events)

    def test_hard_limit_event_attempted_action(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="hard_action")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        events = context.event_logger.get_events()
        hard_events = [e for e in events if "Hard limit" in e.detail]
        assert all(e.attempted_action == "trust_level_hard_limit_check" for e in hard_events)

    def test_no_introspection_events_from_hard_limits(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="no_intro_hard")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        introspection_events = context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(introspection_events) == 0

    def test_trusted_limited_hard_limit_category(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_hard",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE


# ═══════════════════════════════════════════════════════════════════════════════
# EDIT 2: activate trust-level validation category correctness
# ═══════════════════════════════════════════════════════════════════════════════


class TestActivateTrustValidationCategory:
    def test_empty_blocked_modules_category_resource(self) -> None:
        policy = SandboxPolicy(plugin_id="empty_blk")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_empty_blocked_not_introspection(self) -> None:
        policy = SandboxPolicy(plugin_id="not_intro_blk")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is not SandboxViolationCategory.INTROSPECTION

    def test_cpu_exceeds_untrusted_validate_category_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="cpu_over",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=90),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_rw_paths_untrusted_validate_category_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="rw_validate",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data"]),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_threads_untrusted_validate_category_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="threads_val",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=4),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_tampered_integrity_category_resource(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper")
        policy.resource_policy.max_cpu_seconds = 9999
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_limited_insufficient_blocked_category_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_blk",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_limited_cpu_exceeds_category_resource(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_cpu",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=200),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_trust_validation_event_logged_with_resource(self) -> None:
        policy = SandboxPolicy(plugin_id="trust_evt")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        events = context.event_logger.get_events()
        trust_events = [e for e in events if "Trust level policy validation failed" in e.detail]
        assert len(trust_events) >= 1
        assert all(e.category is SandboxViolationCategory.RESOURCE for e in trust_events)

    def test_trust_validation_no_introspection_events(self) -> None:
        policy = SandboxPolicy(plugin_id="trust_no_intro")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        intro_events = context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(intro_events) == 0

    def test_trust_validation_exception_detail_contains_trust_level(self) -> None:
        policy = SandboxPolicy(plugin_id="detail_trust", trust_level="untrusted")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert "untrusted" in exc_info.value.detail

    def test_trust_validation_attempted_action(self) -> None:
        policy = SandboxPolicy(plugin_id="trust_action")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.attempted_action == "trust_level_validation"

    def test_trust_validation_plugin_id_propagated(self) -> None:
        policy = SandboxPolicy(plugin_id="trust_pid_test")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.plugin_id == "trust_pid_test"

    def test_trust_validated_before_hard_limits(self) -> None:
        policy = SandboxPolicy(
            plugin_id="order_check",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=set()),
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert "Trust level policy validation failed" in exc_info.value.detail


# ═══════════════════════════════════════════════════════════════════════════════
# EDIT 3: executor._evaluate_inner cleanup on SandboxViolation
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutorCleanupOnSandboxViolation:
    async def test_cleanup_called_once_per_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="once_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock:
            await executor.safe_evaluate(None, None, None)
            mock.assert_called_once()

    async def test_cleanup_called_before_returning(self) -> None:
        policy = SandboxPolicy(plugin_id="order_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        call_order = []
        original_cleanup = executor._context.cleanup

        def tracking_cleanup():
            call_order.append("cleanup")
            return original_cleanup()

        with patch.object(executor._context, "cleanup", side_effect=tracking_cleanup):
            result = await executor.safe_evaluate(None, None, None)
            call_order.append("return")

        assert call_order.index("cleanup") < call_order.index("return")
        assert result == []

    async def test_no_cleanup_on_successful_activate(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "ok_no_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock:
            await executor.safe_evaluate(None, None, None)
            mock.assert_not_called()
        executor.cleanup()

    async def test_metrics_recorded_on_violation_with_cleanup(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="met_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)
        await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("met_clean")
        assert metrics is not None
        assert metrics["errors"] == 1

    async def test_context_inactive_after_violation_cleanup(self) -> None:
        policy = SandboxPolicy(plugin_id="inactive_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        await executor.safe_evaluate(None, None, None)
        assert executor._context.is_active is False

    async def test_multiple_calls_each_trigger_cleanup(self) -> None:
        policy = SandboxPolicy(plugin_id="multi_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock:
            for _ in range(5):
                await executor.safe_evaluate(None, None, None)
            assert mock.call_count == 5

    async def test_successful_eval_after_violation_no_leaked_state(self) -> None:
        bad_policy = SandboxPolicy(plugin_id="leak_bad")
        executor = PluginSandboxExecutor(_StubStrategy(), bad_policy)
        result1 = await executor.safe_evaluate(None, None, None)
        assert result1 == []
        assert executor._context.is_active is False
        executor.cleanup()

        good_policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "leak_good")
        good_executor = PluginSandboxExecutor(_SignalStrategy(), good_policy)
        try:
            result2 = await good_executor.safe_evaluate(None, None, None)
            assert len(result2) == 1
        finally:
            good_executor.cleanup()

    async def test_executor_cleanup_public_method(self) -> None:
        policy = SandboxPolicy(plugin_id="pub_clean")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        with patch.object(executor._context, "cleanup") as mock:
            executor.cleanup()
            mock.assert_called_once()

    async def test_error_message_in_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="err_msg")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)
        await executor.safe_evaluate(None, None, None)
        metrics = collector.get_plugin_metrics("err_msg")
        assert metrics["last_error"] is not None
        assert "Trust level policy validation failed" in metrics["last_error"]


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: all three edits working together
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreeEditsIntegration:
    def test_hard_limit_violation_category_in_event_and_exception(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="integ_hard")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()

        assert exc_info.value.category is SandboxViolationCategory.RESOURCE
        events = context.event_logger.get_events(category=SandboxViolationCategory.RESOURCE)
        assert len(events) >= 1
        assert all(e.category is SandboxViolationCategory.RESOURCE for e in events)
        intro_events = context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(intro_events) == 0

    def test_trust_validation_category_in_event_and_exception(self) -> None:
        policy = SandboxPolicy(plugin_id="integ_trust")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()

        assert exc_info.value.category is SandboxViolationCategory.RESOURCE
        events = context.event_logger.get_events(category=SandboxViolationCategory.RESOURCE)
        assert len(events) >= 1
        intro_events = context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(intro_events) == 0

    async def test_executor_violation_triggers_cleanup_and_resource_events(self) -> None:
        collector = SandboxMetricsCollector()
        policy = SandboxPolicy(plugin_id="integ_exec")
        executor = PluginSandboxExecutor(_StubStrategy(), policy, metrics_collector=collector)

        with patch.object(executor._context, "cleanup", wraps=executor._context.cleanup) as mock_cleanup:
            result = await executor.safe_evaluate(None, None, None)

        assert result == []
        mock_cleanup.assert_called_once()
        assert executor._context.is_active is False

        events = executor._context.event_logger.get_events(category=SandboxViolationCategory.RESOURCE)
        assert len(events) >= 1
        intro_events = executor._context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(intro_events) == 0

        metrics = collector.get_plugin_metrics("integ_exec")
        assert metrics["errors"] == 1

    async def test_valid_policy_full_pipeline_no_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "full_ok")
        executor = PluginSandboxExecutor(_SignalStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "TSLA"
            events = executor._context.event_logger.get_events()
            assert len(events) == 0
        finally:
            executor.cleanup()

    def test_activate_validation_before_hard_limits_ordering(self) -> None:
        policy = SandboxPolicy(
            plugin_id="ordering",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=set()),
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert "Trust level policy validation failed" in exc_info.value.detail

    def test_hard_limits_only_when_validation_passes(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_only",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=10 * 1024**3),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert "Hard limit" in exc_info.value.detail

    async def test_executor_factory_creates_valid_executor(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "factory_ok")

        def factory():
            return _StubStrategy()

        executor = PluginSandboxExecutor.from_factory(factory, policy)
        assert executor.strategy.name == "stub"
        signals = await executor.safe_evaluate(None, None, None)
        assert signals == []
        executor.cleanup()


# ═══════════════════════════════════════════════════════════════════════════════
# Boundary and edge case tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBoundaryConditions:
    def test_cpu_at_exact_untrusted_validate_limit_ok(self) -> None:
        policy = _valid_untrusted_policy(max_cpu_seconds=60.0)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("60.0 should be valid for untrusted")
        finally:
            context.cleanup()

    def test_cpu_just_over_untrusted_validate_limit_fails(self) -> None:
        policy = _valid_untrusted_policy(max_cpu_seconds=60.01)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_memory_at_exact_hard_limit_ok(self) -> None:
        policy = _valid_untrusted_policy(max_memory_bytes=1024**3)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("1GB should be at exact hard limit for untrusted")
        finally:
            context.cleanup()

    def test_memory_just_over_hard_limit_fails(self) -> None:
        policy = _valid_untrusted_policy(max_memory_bytes=1024**3 + 1)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_blocked_modules_at_exact_minimum(self) -> None:
        policy = _valid_untrusted_policy(blocked_count=10)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("10 modules should be minimum for untrusted")
        finally:
            context.cleanup()

    def test_blocked_modules_one_below_minimum(self) -> None:
        policy = _valid_untrusted_policy(blocked_count=9)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_threads_at_one_ok(self) -> None:
        policy = _valid_untrusted_policy(max_threads=1)
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("1 thread should be allowed for untrusted")
        finally:
            context.cleanup()

    def test_threads_at_two_fails(self) -> None:
        policy = _valid_untrusted_policy(max_threads=2)
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_empty_rw_paths_ok_for_untrusted(self) -> None:
        policy = _valid_untrusted_policy(read_write_paths=[])
        context = SandboxContext(policy)
        try:
            context.activate()
        except SandboxViolation:
            pytest.fail("Empty rw paths should be fine for untrusted")
        finally:
            context.cleanup()

    def test_nonempty_rw_paths_fails_for_untrusted(self) -> None:
        policy = _valid_untrusted_policy(read_write_paths=["/tmp"])  # noqa: S108
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    def test_activate_idempotent(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "idempotent")
        context = SandboxContext(policy)
        try:
            context.activate()
            assert context.is_active is True
            context.activate()
            assert context.is_active is True
        finally:
            context.cleanup()

    def test_deactivate_when_inactive_noop(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "deact_noop")
        context = SandboxContext(policy)
        context.deactivate()
        assert context.is_active is False

    def test_invalid_trust_level_defaults_untrusted(self) -> None:
        policy = SandboxPolicy(plugin_id="bad_trust", trust_level="nonexistent")
        context = SandboxContext(policy)
        assert context.trust_level == TrustLevel.UNTRUSTED


# ═══════════════════════════════════════════════════════════════════════════════
# Context manager and lifecycle tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextManagerLifecycle:
    def test_context_manager_activate_deactivate(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "ctx_mgr")
        with SandboxContext(policy) as ctx:
            assert ctx.is_active is True
        assert ctx.is_active is False

    def test_context_manager_violation_propagates(self) -> None:
        policy = SandboxPolicy(plugin_id="ctx_viol")
        with pytest.raises(SandboxViolation), SandboxContext(policy):
            pass

    def test_cleanup_after_context_manager(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "ctx_clean")
        ctx = SandboxContext(policy)
        with ctx:
            pass
        ctx.cleanup()
        assert ctx.is_active is False

    def test_context_activate_fails_deactivate_still_safe(self) -> None:
        policy = SandboxPolicy(plugin_id="fail_safe")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        context.deactivate()
        context.cleanup()
        assert context.is_active is False


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: ensure old INTROSPECTION category is NOT used
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoIntrospectionCategoryRegression:
    def test_no_introspection_events_for_any_context_violation(self) -> None:
        policies_triggering_violations = [
            SandboxPolicy(plugin_id="empty"),
            _invalid_untrusted_policy_violates_hard(plugin_id="mem"),
            SandboxPolicy(
                plugin_id="cpu",
                trust_level="untrusted",
                import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
                resource_policy=ResourcePolicy(max_cpu_seconds=9999),
            ),
            SandboxPolicy(
                plugin_id="threads",
                trust_level="untrusted",
                import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
                resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=8),
            ),
        ]
        for policy in policies_triggering_violations:
            if not hasattr(policy, "_integrity_hash") or policy._integrity_hash is None:
                policy.set_integrity_hash()
            context = SandboxContext(policy)
            with pytest.raises(SandboxViolation):
                context.activate()
            intro_events = context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
            assert len(intro_events) == 0, f"INTROSPECTION events found for {policy.plugin_id}"

    async def test_no_introspection_events_in_executor_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="exec_no_intro")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        await executor.safe_evaluate(None, None, None)
        intro_events = executor._context.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(intro_events) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Exception structure and to_dict tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestViolationStructureFromEdits:
    def test_hard_limit_violation_to_dict(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="dict_hard")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        d = exc_info.value.to_dict()
        assert d["category"] == "resource"
        assert d["plugin_id"] == "dict_hard"
        assert "Hard limit" in d["detail"]
        assert d["attempted_action"] == "trust_level_hard_limit_check"

    def test_trust_validation_violation_to_dict(self) -> None:
        policy = SandboxPolicy(plugin_id="dict_trust")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        d = exc_info.value.to_dict()
        assert d["category"] == "resource"
        assert d["plugin_id"] == "dict_trust"
        assert "Trust level policy validation failed" in d["detail"]
        assert d["attempted_action"] == "trust_level_validation"

    def test_exception_is_instance_of_sandbox_violation(self) -> None:
        policies = [
            SandboxPolicy(plugin_id="ex1"),
            _invalid_untrusted_policy_violates_hard(plugin_id="ex2"),
        ]
        for policy in policies:
            policy.set_integrity_hash()
            context = SandboxContext(policy)
            with pytest.raises(SandboxViolation):
                context.activate()


# ═══════════════════════════════════════════════════════════════════════════════
# Trust level interaction tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrustLevelInteractions:
    def test_trusted_full_policy_activates(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "full_trust")
        context = SandboxContext(policy)
        try:
            context.activate()
            assert context.is_active is True
        finally:
            context.cleanup()

    def test_trusted_limited_policy_activates(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "limited_trust")
        context = SandboxContext(policy)
        try:
            context.activate()
            assert context.is_active is True
        finally:
            context.cleanup()

    def test_trusted_full_with_memory_over_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="full_mem_over",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules={"subprocess", "ctypes", "_ctypes"}),
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        policy.set_integrity_hash()
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            context.activate()
        assert exc_info.value.category is SandboxViolationCategory.RESOURCE

    async def test_executor_trusted_full_success(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "exec_full")
        executor = PluginSandboxExecutor(_SignalStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
        finally:
            executor.cleanup()

    async def test_executor_trusted_limited_success(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "exec_limited")
        executor = PluginSandboxExecutor(_StubStrategy(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()


# ═══════════════════════════════════════════════════════════════════════════════
# Event logger interaction tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventLoggerInteraction:
    def test_event_count_after_hard_limit_violation(self) -> None:
        policy = _invalid_untrusted_policy_violates_hard(plugin_id="evt_count")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        assert context.event_logger.event_count >= 1

    def test_event_count_after_trust_validation_violation(self) -> None:
        policy = SandboxPolicy(plugin_id="evt_trust_cnt")
        context = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            context.activate()
        assert context.event_logger.event_count >= 1

    def test_no_events_on_successful_activation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "evt_ok")
        context = SandboxContext(policy)
        try:
            context.activate()
            assert context.event_logger.event_count == 0
        finally:
            context.cleanup()

    def test_events_since_returns_recent(self) -> None:
        import time
        policy = SandboxPolicy(plugin_id="evt_since")
        context = SandboxContext(policy)
        before = time.time()
        with pytest.raises(SandboxViolation):
            context.activate()
        after = time.time()
        events = context.event_logger.get_events_since(before)
        assert len(events) >= 1
        events_since_after = context.event_logger.get_events_since(after + 100)
        assert len(events_since_after) == 0
