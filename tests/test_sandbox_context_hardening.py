"""Tests for sandbox context hardening changes.

Covers three focused changes to engine/plugins/sandbox/core/context.py:

1. activate() rollback: layer.install() loop wrapped in try/except that
   calls layer.uninstall() on already-installed layers on failure.
2. Trust-level error message: uses self._trust_level (resolved TrustLevel
   enum) instead of self._policy.trust_level (raw string).
3. _enforce_hard_limits intentional hardening: raises SandboxViolation
   rather than silently logging, with docstring documenting the rationale.
"""

from __future__ import annotations

import builtins
from typing import Any
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
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import SandboxViolation, SandboxViolationCategory
from engine.plugins.trust_levels import TrustLevel


def _make_untrusted_policy(**overrides: Any) -> SandboxPolicy:
    defaults: dict[str, Any] = {
        "plugin_id": "test_rollback",
        "trust_level": "untrusted",
        "import_policy": ImportPolicy(
            blocked_modules={f"mod_{i}" for i in range(15)},
        ),
        "resource_policy": ResourcePolicy(
            max_cpu_seconds=30,
            max_threads=1,
        ),
        "filesystem_policy": FilesystemPolicy(read_write_paths=[]),
        "network_policy": NetworkPolicy(block_metadata_endpoints=True),
        "introspection_policy": IntrospectionPolicy(),
    }
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


# ─── 1. activate() rollback on layer.install() failure ──────────────


class TestActivateRollback:
    """Verify that when a layer.install() fails mid-sequence, all
    already-installed layers are uninstalled before the exception
    propagates.
    """

    def test_network_install_failure_uninstalls_nothing(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        original_open = builtins.open
        with (
            patch.object(ctx._network_layer, "install", side_effect=RuntimeError("net fail")),
            pytest.raises(RuntimeError, match="net fail"),
        ):
            ctx.activate()
        assert ctx.is_active is False
        assert builtins.open is original_open

    def test_resource_install_failure_uninstalls_network(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        with (
            patch.object(ctx._resource_layer, "install", side_effect=RuntimeError("res fail")),
            pytest.raises(RuntimeError, match="res fail"),
        ):
            ctx.activate()
        assert ctx.is_active is False
        assert ctx._network_layer._installed is False

    def test_filesystem_install_failure_uninstalls_previous(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        with patch.object(
            ctx._filesystem_layer, "install", side_effect=RuntimeError("fs fail")
        ), pytest.raises(RuntimeError, match="fs fail"):
            ctx.activate()
        assert ctx.is_active is False
        assert ctx._network_layer._installed is False
        assert ctx._resource_layer._installed is False

    def test_introspection_install_failure_uninstalls_previous(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        original_getattr = builtins.getattr
        with patch.object(
            ctx._introspection_layer, "install", side_effect=RuntimeError("intro fail")
        ), pytest.raises(RuntimeError, match="intro fail"):
            ctx.activate()
        assert ctx.is_active is False
        assert ctx._network_layer._installed is False
        assert ctx._resource_layer._installed is False
        assert ctx._filesystem_layer._installed is False
        assert builtins.getattr is original_getattr

    def test_import_install_failure_uninstalls_all_previous(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        original_import = builtins.__import__
        original_getattr = builtins.getattr
        with patch.object(
            ctx._import_layer, "install", side_effect=RuntimeError("import fail")
        ), pytest.raises(RuntimeError, match="import fail"):
            ctx.activate()
        assert ctx.is_active is False
        assert ctx._network_layer._installed is False
        assert ctx._resource_layer._installed is False
        assert ctx._filesystem_layer._installed is False
        assert ctx._introspection_layer._installed is False
        assert ctx._import_layer._installed is False
        assert builtins.__import__ is original_import
        assert builtins.getattr is original_getattr

    def test_successful_activation_no_rollback(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()
        assert ctx.is_active is False

    def test_rollback_preserves_original_exception_type(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)

        class CustomInstallError(Exception):
            pass

        with patch.object(
            ctx._resource_layer, "install", side_effect=CustomInstallError("boom")
        ), pytest.raises(CustomInstallError, match="boom"):
            ctx.activate()

    def test_rollback_with_sandbox_violation_during_install(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        with patch.object(
            ctx._resource_layer,
            "install",
            side_effect=SandboxViolation(
                "install violation",
                category=SandboxViolationCategory.RESOURCE,
                plugin_id="test_rollback",
                attempted_action="install_resource",
            ),
        ), pytest.raises(SandboxViolation, match="install violation"):
            ctx.activate()
        assert ctx.is_active is False

    def test_multiple_activate_failures_are_independent(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        original_import = builtins.__import__
        for _ in range(3):
            with patch.object(
                ctx._import_layer, "install", side_effect=RuntimeError("fail")
            ), pytest.raises(RuntimeError):
                ctx.activate()
            assert ctx.is_active is False
        assert builtins.__import__ is original_import


# ─── 2. Trust level error message uses self._trust_level ────────────


class TestTrustLevelErrorMessage:
    """Verify that the SandboxViolation raised by validate_trust_level
    failure references the resolved TrustLevel enum value, not the raw
    policy string.
    """

    def test_error_message_shows_resolved_trust_level_untrusted(self) -> None:
        policy = _make_untrusted_policy()
        policy.import_policy.blocked_modules = set()
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        assert "TrustLevel.UNTRUSTED" in str(exc_info.value)

    def test_error_message_uses_enum_not_raw_string(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test_msg",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx._policy.trust_level == "untrusted"
        assert ctx._trust_level == TrustLevel.UNTRUSTED
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        msg = str(exc_info.value)
        assert "TrustLevel.UNTRUSTED" in msg
        assert "untrusted" not in msg or "TrustLevel.UNTRUSTED" in msg

    def test_invalid_trust_level_shows_resolved_enum(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test_invalid",
            trust_level="totally_bogus",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx._policy.trust_level == "totally_bogus"
        assert ctx._trust_level == TrustLevel.UNTRUSTED
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        msg = str(exc_info.value)
        assert "TrustLevel.UNTRUSTED" in msg
        assert "totally_bogus" not in msg

    def test_event_log_uses_resolved_trust_level(self) -> None:
        policy = _make_untrusted_policy()
        policy.import_policy.blocked_modules = set()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        events = ctx.event_logger.get_events(
            category=SandboxViolationCategory.INTROSPECTION
        )
        assert len(events) >= 1
        detail = events[0].detail
        assert "TrustLevel.UNTRUSTED" in detail

    def test_trusted_limited_shows_enum_in_error(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test_limited",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx._trust_level == TrustLevel.TRUSTED_LIMITED
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        assert "TrustLevel.TRUSTED_LIMITED" in str(exc_info.value)

    def test_violation_metadata_on_trust_failure(self) -> None:
        policy = _make_untrusted_policy()
        policy.import_policy.blocked_modules = set()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx.activate()
        v = exc_info.value
        assert v.category == SandboxViolationCategory.INTROSPECTION
        assert v.plugin_id == "test_rollback"
        assert v.attempted_action == "trust_level_validation"


# ─── 3. _enforce_hard_limits raises SandboxViolation (hardening) ────


class TestEnforceHardLimitsHardening:
    """Verify that _enforce_hard_limits raises SandboxViolation rather than
    silently logging.  This is the intentional hardening from soft-log to
    hard-raise.
    """

    def test_cpu_exceeded_raises_violation(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="max_cpu_seconds"):
            ctx._enforce_hard_limits()

    def test_memory_exceeded_raises_violation(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="max_memory_bytes"):
            ctx._enforce_hard_limits()

    def test_untrusted_with_write_paths_raises(self) -> None:
        policy = _make_untrusted_policy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="write paths"):
            ctx._enforce_hard_limits()

    def test_untrusted_with_threads_raises(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_threads=4),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="threads"):
            ctx._enforce_hard_limits()

    def test_untrusted_no_metadata_block_raises(self) -> None:
        policy = _make_untrusted_policy(
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="metadata"):
            ctx._enforce_hard_limits()

    def test_no_violations_passes_silently(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()

    def test_violation_event_logged_before_raise(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx._enforce_hard_limits()
        events = ctx.event_logger.get_events(
            category=SandboxViolationCategory.INTROSPECTION
        )
        assert len(events) >= 1
        assert "Hard limit violations" in events[0].detail
        assert "max_cpu_seconds" in events[0].detail

    def test_violation_category_is_introspection(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert exc_info.value.category == SandboxViolationCategory.INTROSPECTION

    def test_violation_includes_plugin_id(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        assert exc_info.value.plugin_id == "test_rollback"

    def test_multiple_violations_in_single_message(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(
                max_cpu_seconds=999,
                max_memory_bytes=10 * 1024**3,
            ),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation) as exc_info:
            ctx._enforce_hard_limits()
        msg = str(exc_info.value)
        assert "max_cpu_seconds" in msg
        assert "max_memory_bytes" in msg

    def test_enforce_hard_limits_called_during_activate(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Hard limit violations"):
            ctx.activate()
        assert ctx.is_active is False

    def test_trusted_full_allows_higher_limits(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test_trusted",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules={"subprocess", "ctypes", "_ctypes"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=300),
        )
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        ctx._enforce_hard_limits()

    def test_enforce_hard_limits_has_docstring(self) -> None:
        assert SandboxContext._enforce_hard_limits.__doc__ is not None
        doc = SandboxContext._enforce_hard_limits.__doc__
        assert "intentional hardening" in doc.lower() or "hardening" in doc.lower()

    def test_docstring_mentions_soft_log_to_raise(self) -> None:
        doc = SandboxContext._enforce_hard_limits.__doc__
        assert doc is not None
        assert "SandboxViolation" in doc
        assert "soft" in doc.lower() or "logged" in doc.lower()


# ─── Edge cases & integration ────────────────────────────────────────


class TestActivateEdgeCases:
    """Edge cases around the activate/rollback/hardening interaction."""

    def test_activate_fails_on_hard_limits_independently(self) -> None:
        policy_high_mem = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        ctx = SandboxContext(policy_high_mem)
        assert ctx.validate_trust_level() is True
        with pytest.raises(SandboxViolation, match="Hard limit"):
            ctx.activate()
        assert ctx.is_active is False

    def test_context_manager_rollback_on_install_failure(self) -> None:
        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        with patch.object(
            ctx._resource_layer, "install", side_effect=RuntimeError("boom")
        ), pytest.raises(RuntimeError, match="boom"), ctx:
            pass
        assert ctx.is_active is False

    def test_trust_level_validation_boundary_untrusted_cpu(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_trust_level_validation_boundary_untrusted_cpu_exceeded(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED + 1),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_trust_level_validation_boundary_limited_modules(self) -> None:
        policy = _make_untrusted_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"m_{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_trust_level_validation_boundary_limited_modules_short(self) -> None:
        policy = _make_untrusted_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"m_{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED - 1)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_read_write_paths_fails_validation(self) -> None:
        policy = _make_untrusted_policy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_threads_gt_one_fails_validation(self) -> None:
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_threads=2),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_min_blocked_modules_passes_validation(self) -> None:
        policy = _make_untrusted_policy(
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED)},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_untrusted_with_below_min_blocked_fails_validation(self) -> None:
        policy = _make_untrusted_policy(
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(_MIN_BLOCKED_MODULES_UNTRUSTED - 1)},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_cpu_boundary_passes(self) -> None:
        policy = _make_untrusted_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"m_{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_limited_cpu_boundary_fails(self) -> None:
        policy = _make_untrusted_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"m_{i}" for i in range(_MIN_BLOCKED_MODULES_LIMITED)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED + 1),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False


class TestMetricsCollectionOnHardLimitViolation:
    """Verify metrics collector is wired correctly when hard limit
    violations trigger rollback paths.
    """

    def test_metrics_not_recorded_on_hard_limits_before_install(self) -> None:
        mock_collector = MagicMock()
        policy = _make_untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy, metrics_collector=mock_collector)
        with pytest.raises(SandboxViolation):
            ctx._enforce_hard_limits()
        mock_collector.record_violation.assert_not_called()

    def test_metrics_recorded_on_deactivate_after_successful_activate(self) -> None:
        mock_collector = MagicMock()
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test_metrics")
        ctx = SandboxContext(policy, metrics_collector=mock_collector)
        ctx.activate()
        try:
            builtins.__import__("os")
        except ImportError:
            pass
        finally:
            ctx.deactivate()
        mock_collector.record_violation.assert_called()
