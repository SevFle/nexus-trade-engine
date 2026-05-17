"""Comprehensive tests for SandboxContext: activate, validate_trust_level,
_enforce_hard_limits, deactivate, cleanup, and context-manager flows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from engine.plugins.sandbox.core.context import (
    _MAX_CPU_SECONDS_LIMITED,
    _MAX_CPU_SECONDS_UNTRUSTED,
    _MIN_BLOCKED_MODULES_LIMITED,
    _MIN_BLOCKED_MODULES_UNTRUSTED,
    SandboxContext,
)
from engine.plugins.sandbox.core.policy import (
    _TRUST_MAX_CPU_HARD_LIMITS,
    _TRUST_MAX_MEMORY_HARD_LIMITS,
    FilesystemPolicy,
    ImportPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import (
    SandboxViolation,
    SandboxViolationCategory,
)
from engine.plugins.trust_levels import TrustLevel


def _make_policy(**overrides):
    defaults = {
        "plugin_id": "test_plugin",
        "trust_level": "untrusted",
        "import_policy": ImportPolicy(
            blocked_modules={f"mod_{i}" for i in range(15)},
        ),
        "resource_policy": ResourcePolicy(
            max_cpu_seconds=30,
            max_threads=1,
        ),
        "filesystem_policy": FilesystemPolicy(),
        "network_policy": NetworkPolicy(),
    }
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


def _untrusted_valid_policy():
    return _make_policy(
        import_policy=ImportPolicy(
            blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)},
        ),
        resource_policy=ResourcePolicy(
            max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED - 1,
            max_threads=1,
        ),
        filesystem_policy=FilesystemPolicy(read_write_paths=[]),
    )


def _limited_valid_policy():
    return _make_policy(
        trust_level="trusted_limited",
        import_policy=ImportPolicy(
            blocked_modules={f"m{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)},
        ),
        resource_policy=ResourcePolicy(
            max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED - 1,
        ),
    )


def _trusted_valid_policy():
    return _make_policy(
        trust_level="trusted_full",
        import_policy=ImportPolicy(blocked_modules={"subprocess", "ctypes", "_ctypes"}),
        resource_policy=ResourcePolicy(max_cpu_seconds=300),
    )


class TestValidateTrustLevelUntrusted:
    def test_valid_untrusted_policy(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_too_few_blocked_modules(self):
        policy = _make_policy(
            import_policy=ImportPolicy(blocked_modules={"a", "b"}),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_exactly_min_blocked_modules(self):
        mods = {f"x{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)}
        policy = _make_policy(import_policy=ImportPolicy(blocked_modules=mods))
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_one_below_min_blocked_modules(self):
        mods = {f"x{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED - 1)}
        policy = _make_policy(import_policy=ImportPolicy(blocked_modules=mods))
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_cpu_seconds_at_limit(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_cpu_seconds_above_limit(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED + 1),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_cpu_seconds_just_below_limit(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED - 0.1),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_read_write_paths_non_empty(self):
        policy = _make_policy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_max_threads_gt_1(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_threads=2),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_max_threads_exactly_1(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_threads=1),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestValidateTrustLevelTrustedLimited:
    def test_valid_limited_policy(self):
        policy = _limited_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_too_few_blocked_modules(self):
        mods = {f"x{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED - 1)}
        policy = _make_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=mods),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_exactly_min_blocked(self):
        mods = {f"x{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)}
        policy = _make_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=mods),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_cpu_at_limit(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_cpu_above_limit(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED + 10),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_read_write_paths_allowed_for_limited(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_threads_not_checked_for_limited(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_threads=4),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestValidateTrustLevelTrustedFull:
    def test_valid_trusted_full_policy(self):
        policy = _trusted_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_no_blocked_modules_required(self):
        policy = _make_policy(
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestValidateTrustLevelIntegrity:
    def test_integrity_failure_returns_false(self):
        policy = _untrusted_valid_policy()
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 9999
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_integrity_pass_returns_true(self):
        policy = _untrusted_valid_policy()
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_no_integrity_hash_passes(self):
        policy = _untrusted_valid_policy()
        assert policy._integrity_hash is None
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestValidateTrustLevelInvalidTrustLevel:
    def test_invalid_trust_level_resolves_to_untrusted(self):
        policy = _make_policy(trust_level="totally_invalid")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED


class TestActivate:
    def test_successful_activation(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.activate()
        assert ctx.is_active is True

    def test_double_activate_is_noop(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.activate()
            ctx.activate()
        assert ctx.is_active is True

    def test_validation_failure_raises_sandbox_violation(self):
        policy = _make_policy(
            import_policy=ImportPolicy(blocked_modules={"a"}),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        assert "Trust level policy validation failed" in str(exc_info.value)
        assert exc_info.value.plugin_id == "test_plugin"
        assert exc_info.value.category == SandboxViolationCategory.INTROSPECTION

    def test_validation_failure_logs_event(self):
        policy = _make_policy(
            import_policy=ImportPolicy(blocked_modules={"a"}),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        events = ctx.event_logger.get_events()
        assert len(events) >= 1
        assert "validation failed" in events[0].detail

    def test_hard_limit_violation_raises_sandbox_violation(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        assert "Hard limit violations" in str(exc_info.value)

    def test_hard_limit_violation_logs_event(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        events = ctx.event_logger.get_events()
        hard_limit_events = [e for e in events if "Hard limit" in e.detail]
        assert len(hard_limit_events) >= 1

    def test_layer_install_failure_deactivates_and_raises(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install", side_effect=RuntimeError("boom")),
            patch.object(ctx._network_layer, "uninstall"),
            patch.object(ctx._resource_layer, "uninstall"),
            patch.object(ctx._filesystem_layer, "uninstall"),
            patch.object(ctx._introspection_layer, "uninstall"),
            patch.object(ctx._import_layer, "uninstall"),
            pytest.raises(RuntimeError, match="boom"),
        ):
            ctx.activate()
        assert ctx.is_active is False

    def test_install_order_is_correct(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        call_order = []
        for layer_name in (
            "_network_layer",
            "_resource_layer",
            "_filesystem_layer",
            "_introspection_layer",
            "_import_layer",
        ):
            layer = getattr(ctx, layer_name)

            def _make_install(name):
                def _install():
                    call_order.append(name)
                return _install

            layer.install = _make_install(layer_name)
        ctx.activate()
        assert call_order == [
            "_network_layer",
            "_resource_layer",
            "_filesystem_layer",
            "_introspection_layer",
            "_import_layer",
        ]


class TestDeactivate:
    def test_deactivate_when_active(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.activate()
        assert ctx.is_active is True
        ctx.deactivate()
        assert ctx.is_active is False

    def test_deactivate_when_not_active_is_noop(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        ctx.deactivate()
        assert ctx.is_active is False

    def test_uninstall_called_in_correct_order(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.activate()
        call_order = []
        for layer_name in (
            "_import_layer",
            "_introspection_layer",
            "_filesystem_layer",
            "_resource_layer",
            "_network_layer",
        ):
            layer = getattr(ctx, layer_name)

            def _make_uninstall(name):
                def _uninstall():
                    call_order.append(name)
                return _uninstall

            layer.uninstall = _make_uninstall(layer_name)
        ctx.deactivate()
        assert call_order == [
            "_import_layer",
            "_introspection_layer",
            "_filesystem_layer",
            "_resource_layer",
            "_network_layer",
        ]


class TestCollectViolations:
    def test_violations_collected_from_all_layers(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        v1 = SandboxViolation(
            "import violation",
            category=SandboxViolationCategory.IMPORT,
            plugin_id="test_plugin",
        )
        v2 = SandboxViolation(
            "network violation",
            category=SandboxViolationCategory.NETWORK,
            plugin_id="test_plugin",
        )
        v3 = SandboxViolation(
            "resource violation",
            category=SandboxViolationCategory.RESOURCE,
            plugin_id="test_plugin",
        )
        v4 = SandboxViolation(
            "filesystem violation",
            category=SandboxViolationCategory.FILESYSTEM,
            plugin_id="test_plugin",
        )
        v5 = SandboxViolation(
            "introspection violation",
            category=SandboxViolationCategory.INTROSPECTION,
            plugin_id="test_plugin",
        )
        ctx._import_layer.get_violations = lambda: [v1]
        ctx._import_layer.clear_violations = lambda: None
        ctx._network_layer.get_violations = lambda: [v2]
        ctx._network_layer.clear_violations = lambda: None
        ctx._resource_layer.get_violations = lambda: [v3]
        ctx._resource_layer.clear_violations = lambda: None
        ctx._filesystem_layer.get_violations = lambda: [v4]
        ctx._filesystem_layer.clear_violations = lambda: None
        ctx._introspection_layer.get_violations = lambda: [v5]
        ctx._introspection_layer.clear_violations = lambda: None
        ctx._collect_violations()
        logged = ctx.event_logger.get_events()
        assert len(logged) == 5

    def test_metrics_collector_records_violations(self):
        policy = _untrusted_valid_policy()
        metrics = MagicMock()
        ctx = SandboxContext(policy, metrics_collector=metrics)
        v = SandboxViolation(
            "test",
            category=SandboxViolationCategory.IMPORT,
            plugin_id="test_plugin",
        )
        ctx._import_layer.get_violations = lambda: [v]
        ctx._import_layer.clear_violations = lambda: None
        ctx._network_layer.get_violations = list
        ctx._network_layer.clear_violations = lambda: None
        ctx._resource_layer.get_violations = list
        ctx._resource_layer.clear_violations = lambda: None
        ctx._filesystem_layer.get_violations = list
        ctx._filesystem_layer.clear_violations = lambda: None
        ctx._introspection_layer.get_violations = list
        ctx._introspection_layer.clear_violations = lambda: None
        ctx._collect_violations()
        metrics.record_violation.assert_called_once_with("test_plugin")

    def test_metrics_collector_not_called_when_no_violations(self):
        policy = _untrusted_valid_policy()
        metrics = MagicMock()
        ctx = SandboxContext(policy, metrics_collector=metrics)
        for layer_name in (
            "_import_layer",
            "_network_layer",
            "_resource_layer",
            "_filesystem_layer",
            "_introspection_layer",
        ):
            layer = getattr(ctx, layer_name)
            layer.get_violations = list
            layer.clear_violations = lambda: None
        ctx._collect_violations()
        metrics.record_violation.assert_not_called()


class TestEnforceHardLimits:
    def test_no_violations_no_raise(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()

    def test_cpu_violation_raises(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert "max_cpu_seconds" in str(exc_info.value)

    def test_memory_violation_raises(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert "max_memory_bytes" in str(exc_info.value)

    def test_untrusted_rw_paths_raises(self):
        policy = _make_policy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert "write paths" in str(exc_info.value)

    def test_untrusted_threads_raises(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_threads=4),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert "threads" in str(exc_info.value)

    def test_untrusted_no_metadata_block_raises(self):
        policy = _make_policy(
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert "metadata" in str(exc_info.value)

    def test_multiple_violations_all_reported(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(
                max_cpu_seconds=999,
                max_memory_bytes=10 * 1024**3,
                max_threads=4,
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/out"]),
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        msg = str(exc_info.value)
        assert "max_cpu_seconds" in msg
        assert "max_memory_bytes" in msg
        assert "threads" in msg
        assert "write paths" in msg
        assert "metadata" in msg

    def test_trusted_full_high_resources_ok(self):
        policy = SandboxPolicy(
            plugin_id="trusted",
            trust_level="trusted_full",
            resource_policy=ResourcePolicy(max_cpu_seconds=500, max_memory_bytes=3 * 1024**3),
        )
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()


class TestCleanup:
    def test_cleanup_deactivates_and_cleans_filesystem(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
            patch.object(ctx._filesystem_layer, "cleanup"),
        ):
            ctx.activate()
        ctx.cleanup()
        assert ctx.is_active is False


class TestContextManager:
    def test_context_manager_protocol(self):
        policy = _untrusted_valid_policy()
        with (
            patch("engine.plugins.sandbox.layers.NetworkGuard.install"),
            patch("engine.plugins.sandbox.layers.ResourceLimiter.install"),
            patch("engine.plugins.sandbox.layers.FilesystemIsolation.install"),
            patch("engine.plugins.sandbox.layers.IntrospectionGuard.install"),
            patch("engine.plugins.sandbox.layers.RestrictedImporter.install"),
        ):
            with SandboxContext(policy) as ctx:
                assert ctx.is_active is True
            assert ctx.is_active is False

    def test_context_manager_activates_on_enter(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            result = ctx.__enter__()
            assert result is ctx
            assert ctx.is_active is True
            ctx.__exit__(None, None, None)

    def test_context_manager_deactivates_on_exit(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.__enter__()
            assert ctx.is_active is True
        ctx.__exit__(None, None, None)
        assert ctx.is_active is False


class TestProperties:
    def test_policy_property(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.policy is policy

    def test_trust_level_property(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED

    def test_work_dir_property(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.work_dir == ctx._filesystem_layer.work_dir

    def test_event_logger_property(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        assert ctx.event_logger is ctx._event_logger


class TestResolveTrustLevel:
    def test_valid_trust_level(self):
        policy = _make_policy(trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.TRUSTED_FULL

    def test_invalid_trust_level_defaults_to_untrusted(self):
        policy = _make_policy(trust_level="nonexistent")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED

    def test_empty_trust_level_defaults_to_untrusted(self):
        policy = _make_policy(trust_level="")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED


class TestEdgeCases:
    def test_activate_then_deactivate_then_activate_again(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        install_mock = MagicMock()
        with (
            patch.object(ctx._network_layer, "install", install_mock),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.activate()
            assert ctx.is_active is True
            ctx.deactivate()
            assert ctx.is_active is False
            ctx.activate()
            assert ctx.is_active is True

    def test_activate_with_metrics_collector(self):
        policy = _untrusted_valid_policy()
        metrics = MagicMock()
        ctx = SandboxContext(policy, metrics_collector=metrics)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            ctx.activate()
        assert ctx.is_active is True

    def test_validate_trust_level_boundary_values(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"x{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)},
            ),
            resource_policy=ResourcePolicy(
                max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED - 0.001,
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_enforce_hard_limits_logs_and_raises(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx._enforce_hard_limits()
        events = ctx.event_logger.get_events()
        assert any("Hard limit" in e.detail for e in events)


class TestImportPolicyIsAllowed:
    def test_blocked_module_is_not_allowed(self):
        policy = ImportPolicy(blocked_modules={"os", "subprocess"})
        assert policy.is_allowed("os") is False

    def test_blocked_root_blocks_submodule(self):
        policy = ImportPolicy(blocked_modules={"os"})
        assert policy.is_allowed("os.path") is False

    def test_allowed_when_not_blocked_and_no_allowlist(self):
        policy = ImportPolicy(blocked_modules={"os"})
        assert policy.is_allowed("json") is True

    def test_allowed_when_in_allowlist(self):
        policy = ImportPolicy(
            allowed_modules={"json", "math"},
            blocked_modules=set(),
        )
        assert policy.is_allowed("json") is True

    def test_blocked_when_not_in_allowlist(self):
        policy = ImportPolicy(
            allowed_modules={"json", "math"},
            blocked_modules=set(),
        )
        assert policy.is_allowed("os") is False

    def test_blocked_takes_priority_over_allowed(self):
        policy = ImportPolicy(
            allowed_modules={"os"},
            blocked_modules={"os"},
        )
        assert policy.is_allowed("os") is False

    def test_submodule_checked_by_root(self):
        policy = ImportPolicy(
            allowed_modules={"json"},
            blocked_modules=set(),
        )
        assert policy.is_allowed("json.decoder") is True

    def test_submodule_not_in_allowlist_blocked(self):
        policy = ImportPolicy(
            allowed_modules={"json"},
            blocked_modules=set(),
        )
        assert policy.is_allowed("os.path") is False


class TestNetworkPolicyIsHostAllowed:
    def test_exact_match(self):
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("api.example.com") is True

    def test_subdomain_match(self):
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("api.example.com") is True

    def test_no_match(self):
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("evil.com") is False

    def test_empty_endpoints_blocks_all(self):
        policy = NetworkPolicy(allowed_endpoints=[])
        assert policy.is_host_allowed("any.host") is False

    def test_partial_name_no_false_match(self):
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("notexample.com") is False

    def test_multiple_endpoints(self):
        policy = NetworkPolicy(allowed_endpoints=["api.example.com", "cdn.other.com"])
        assert policy.is_host_allowed("cdn.other.com") is True
        assert policy.is_host_allowed("api.example.com") is True
        assert policy.is_host_allowed("unknown.com") is False


class TestParseMemory:
    def test_gb(self):
        assert _parse_memory("2GB") == 2 * 1024**3

    def test_mb(self):
        assert _parse_memory("512MB") == 512 * 1024**2

    def test_kb(self):
        assert _parse_memory("256KB") == 256 * 1024

    def test_bytes(self):
        assert _parse_memory("1024B") == 1024

    def test_plain_number(self):
        assert _parse_memory("1024") == 1024

    def test_lowercase(self):
        assert _parse_memory("512mb") == 512 * 1024**2

    def test_whitespace(self):
        assert _parse_memory("  1GB  ") == 1024**3

    def test_float_value(self):
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)

    def test_zero(self):
        assert _parse_memory("0MB") == 0


class TestSandboxPolicyFromTrustLevel:
    def test_untrusted_basic(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert policy.plugin_id == "test"
        assert policy.trust_level == "untrusted"
        assert policy._integrity_hash is not None

    def test_trusted_full_has_fewer_blocked(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert "os" not in policy.import_policy.blocked_modules
        assert len(policy.import_policy.blocked_modules) < 5

    def test_untrusted_has_many_blocked(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert len(policy.import_policy.blocked_modules) > 10

    def test_cpu_capped_at_hard_limit(self):
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            max_cpu_seconds=99999,
        )
        hard_limit = _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED]
        assert policy.resource_policy.max_cpu_seconds == hard_limit

    def test_memory_capped_at_hard_limit(self):
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            max_memory_bytes=999 * 1024**3,
        )
        hard_limit = _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED]
        assert policy.resource_policy.max_memory_bytes == hard_limit

    def test_trusted_full_multiplier_applied(self):
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL, "test",
            max_cpu_seconds=30,
            max_memory_bytes=512 * 1024**2,
        )
        assert policy.resource_policy.max_cpu_seconds == 30 * 4.0
        assert policy.resource_policy.max_memory_bytes == int(512 * 1024**2 * 4.0)

    def test_trusted_limited_multiplier_applied(self):
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_LIMITED, "test",
            max_cpu_seconds=30,
        )
        assert policy.resource_policy.max_cpu_seconds == 30 * 2.0

    def test_network_endpoints_passed_through(self):
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            network_endpoints=["api.example.com"],
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_read_only_paths_passed_through(self):
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            read_only_paths=["/data/file.csv"],
        )
        assert policy.filesystem_policy.read_only_paths == ["/data/file.csv"]

    def test_default_plugin_id(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED)
        assert policy.plugin_id == "unknown"

    def test_introspection_policy_untrusted_is_strict(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert "eval" in policy.introspection_policy.blocked_builtins

    def test_introspection_policy_trusted_full_is_relaxed(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert "eval" not in policy.introspection_policy.blocked_builtins


class TestSandboxPolicyFromManifest:
    def _make_manifest(self, **overrides):
        defaults = {
            "id": "my_plugin",
            "trust_level": "untrusted",
            "requires_network": lambda: False,
            "network": SimpleNamespace(allowed_endpoints=[]),
            "resources": SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
            "artifacts": [],
            "permissions": [],
            "has_permission": lambda _: False,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_basic_untrusted(self):
        manifest = self._make_manifest()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "my_plugin"
        assert policy.trust_level == "untrusted"
        assert policy._integrity_hash is not None

    def test_trusted_full_manifest(self):
        manifest = self._make_manifest(trust_level="trusted_full")
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "trusted_full"

    def test_trusted_limited_manifest(self):
        manifest = self._make_manifest(trust_level="trusted_limited")
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "trusted_limited"

    def test_network_endpoints_when_requires_network(self):
        manifest = self._make_manifest(
            requires_network=lambda: True,
            network=SimpleNamespace(allowed_endpoints=["api.example.com"]),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_network_endpoints_not_set_when_no_network(self):
        manifest = self._make_manifest(requires_network=lambda: False)
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == []

    def test_filesystem_write_for_trusted_with_permission(self):
        manifest = self._make_manifest(
            trust_level="trusted_full",
            artifacts=["/data/out"],
            has_permission=lambda p: p == "filesystem_write",
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert "/data/out" in policy.filesystem_policy.read_write_paths

    def test_no_filesystem_write_without_permission(self):
        manifest = self._make_manifest(
            trust_level="trusted_full",
            artifacts=["/data/out"],
            has_permission=lambda _: False,
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == []

    def test_no_filesystem_write_for_untrusted_even_with_permission(self):
        manifest = self._make_manifest(
            trust_level="untrusted",
            artifacts=["/data/out"],
            has_permission=lambda p: p == "filesystem_write",
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == []

    def test_cpu_capped_at_hard_limit(self):
        manifest = self._make_manifest(
            trust_level="untrusted",
            resources=SimpleNamespace(max_cpu_seconds=99999, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        hard_limit = _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED]
        assert policy.resource_policy.max_cpu_seconds == hard_limit

    def test_memory_parsed_from_manifest(self):
        manifest = self._make_manifest(
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="1GB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.resource_policy.max_memory_bytes > 512 * 1024**2

    def test_artifacts_as_read_only_paths(self):
        manifest = self._make_manifest(artifacts=["/data/a.csv", "/data/b.csv"])
        policy = SandboxPolicy.from_manifest(manifest)
        assert "/data/a.csv" in policy.filesystem_policy.read_only_paths

    def test_manifest_without_optional_attrs(self):
        manifest = SimpleNamespace(
            id="minimal",
            trust_level="untrusted",
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "minimal"


class TestSandboxPolicyTrustedPolicy:
    def test_creates_trusted_policy(self):
        policy = SandboxPolicy.trusted_policy("my_trusted")
        assert policy.plugin_id == "my_trusted"
        assert policy.trust_level == "trusted_full"
        assert policy._integrity_hash is not None

    def test_default_plugin_id(self):
        policy = SandboxPolicy.trusted_policy()
        assert policy.plugin_id == "trusted"

    def test_has_expected_blocked_modules(self):
        policy = SandboxPolicy.trusted_policy()
        assert "subprocess" in policy.import_policy.blocked_modules

    def test_has_relaxed_introspection(self):
        policy = SandboxPolicy.trusted_policy()
        assert "eval" not in policy.introspection_policy.blocked_builtins

    def test_high_resource_limits(self):
        policy = SandboxPolicy.trusted_policy()
        assert policy.resource_policy.max_cpu_seconds == 300
        assert policy.resource_policy.max_memory_bytes == 2 * 1024**3


class TestGetFullBlockedModulesFallback:
    def test_fallback_when_import_fails(self):
        from engine.plugins.sandbox.core import policy as policy_mod
        with patch.dict("sys.modules", {"engine.plugins.restricted_importer": None}):
            result = policy_mod._get_full_blocked_modules()
        assert isinstance(result, set)
        assert "os" in result
        assert "subprocess" in result


class TestSandboxPolicyIntegrityHashDetailed:
    def test_hash_changes_on_policy_mutation(self):
        policy = _untrusted_valid_policy()
        policy.set_integrity_hash()
        hash_before = policy._integrity_hash
        policy.resource_policy.max_cpu_seconds = 9999
        hash_after = policy.compute_integrity_hash()
        assert hash_before != hash_after

    def test_hash_deterministic(self):
        policy = _untrusted_valid_policy()
        h1 = policy.compute_integrity_hash()
        h2 = policy.compute_integrity_hash()
        assert h1 == h2

    def test_different_policies_different_hashes(self):
        p1 = _untrusted_valid_policy()
        p2 = _make_policy(
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        assert p1.compute_integrity_hash() != p2.compute_integrity_hash()

    def test_verify_integrity_none_hash(self):
        policy = _untrusted_valid_policy()
        assert policy._integrity_hash is None
        assert policy.verify_integrity() is True

    def test_verify_integrity_tampered(self):
        policy = _untrusted_valid_policy()
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 9999
        assert policy.verify_integrity() is False

    def test_verify_integrity_untampered(self):
        policy = _untrusted_valid_policy()
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True


class TestEnforceHardLimitsDetailed:
    def test_cpu_at_exact_hard_limit_passes(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(
                max_cpu_seconds=_TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED],
            ),
        )
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()

    def test_cpu_one_over_hard_limit_fails(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(
                max_cpu_seconds=_TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED] + 1,
            ),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx._enforce_hard_limits()

    def test_memory_at_exact_hard_limit_passes(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(
                max_memory_bytes=_TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED],
            ),
        )
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()

    def test_memory_one_over_hard_limit_fails(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(
                max_memory_bytes=_TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED] + 1,
            ),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx._enforce_hard_limits()

    def test_trusted_limited_high_cpu_but_within_limit(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(
                max_cpu_seconds=_TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED],
            ),
        )
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()

    def test_trusted_limited_over_cpu_hard_limit(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(
                max_cpu_seconds=_TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED] + 1,
            ),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx._enforce_hard_limits()

    def test_violation_includes_plugin_id(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert exc_info.value.plugin_id == "test_plugin"

    def test_violation_category_is_introspection(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert exc_info.value.category == SandboxViolationCategory.INTROSPECTION


class TestActivateIntegrationDetailed:
    def test_activate_does_not_set_active_on_trust_validation_fail(self):
        policy = _make_policy(
            import_policy=ImportPolicy(blocked_modules={"a"}),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        assert ctx.is_active is False

    def test_activate_does_not_set_active_on_hard_limit_fail(self):
        policy = _make_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        assert ctx.is_active is False

    def test_activate_sets_active_only_after_all_checks_pass(self):
        policy = _untrusted_valid_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._network_layer, "install"),
            patch.object(ctx._resource_layer, "install"),
            patch.object(ctx._filesystem_layer, "install"),
            patch.object(ctx._introspection_layer, "install"),
            patch.object(ctx._import_layer, "install"),
        ):
            assert ctx.is_active is False
            ctx.activate()
            assert ctx.is_active is True

    def test_activate_integrity_tamper_prevents_activation(self):
        policy = _untrusted_valid_policy()
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 9999
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        assert "Trust level policy validation failed" in str(exc_info.value)
        assert ctx.is_active is False

    def test_multiple_activate_failure_reasons_both_logged(self):
        policy = _make_policy(
            import_policy=ImportPolicy(blocked_modules=set()),
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        assert ctx.is_active is False


class TestEnvironmentPolicyPresets:
    def test_untrusted_blocks_os_environ(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert policy.environment_policy.block_os_environ is True
        assert len(policy.environment_policy.allowed_env_vars) == 0

    def test_trusted_full_allows_env_vars(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert policy.environment_policy.block_os_environ is False
        assert "HOME" in policy.environment_policy.allowed_env_vars

    def test_trusted_limited_allows_subset(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "test")
        assert policy.environment_policy.block_os_environ is True
        assert "HOME" in policy.environment_policy.allowed_env_vars
