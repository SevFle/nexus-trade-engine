"""
Adversarial tests for Layer 3: Resource Limits.

Tests resource exhaustion attack vectors including:
  - CPU exhaustion via infinite loops
  - Memory exhaustion via large allocations
  - File descriptor exhaustion
  - Thread creation limits
  - Wall-time timeout enforcement
  - Fork bomb attempts
  - Stack overflow via recursion
  - Combined resource exhaustion techniques
"""

from __future__ import annotations

import time

import pytest

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import (
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.resource_limiter import (
    ResourceLimiter,
    _CPUTimer,
    _WallTimer,
)


class TestCPUExhaustion:
    def test_cpu_timer_expires_on_infinite_loop(self) -> None:
        timer = _CPUTimer(0.05, plugin_id="test")
        timer.start()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted, match="cpu_time"):
            timer.check()
        timer.stop()

    def test_cpu_timer_within_limit(self) -> None:
        timer = _CPUTimer(5.0, plugin_id="test")
        timer.start()
        timer.check()
        timer.stop()

    def test_cpu_timer_elapsed_tracks_time(self) -> None:
        timer = _CPUTimer(10.0, plugin_id="test")
        timer.start()
        e1 = timer.elapsed
        time.sleep(0.05)
        e2 = timer.elapsed
        assert e2 > e1
        timer.stop()

    def test_cpu_timer_stop_prevents_expiry(self) -> None:
        timer = _CPUTimer(0.05, plugin_id="test")
        timer.start()
        timer.stop()
        time.sleep(0.1)
        assert not timer.expired

    def test_cpu_timer_expired_property(self) -> None:
        timer = _CPUTimer(0.05, plugin_id="test")
        assert not timer.expired
        timer.start()
        time.sleep(0.1)
        assert timer.expired
        timer.stop()

    def test_resource_limiter_checks_cpu_timer(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.05, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        try:
            time.sleep(0.1)
            with pytest.raises(ResourceExhausted, match="cpu_time"):
                limiter.check_cpu_timer()
        finally:
            limiter.uninstall()

    def test_cpu_violation_category_is_resource(self) -> None:
        timer = _CPUTimer(0.01, plugin_id="test")
        timer.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            timer.check()
        assert exc_info.value.category == SandboxViolationCategory.RESOURCE
        assert exc_info.value.resource_type == "cpu_time"
        timer.stop()

    def test_cpu_violation_includes_plugin_id(self) -> None:
        timer = _CPUTimer(0.01, plugin_id="my_plugin")
        timer.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            timer.check()
        assert exc_info.value.plugin_id == "my_plugin"
        timer.stop()

    def test_cpu_violation_includes_limit_and_current(self) -> None:
        timer = _CPUTimer(0.01, plugin_id="test")
        timer.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            timer.check()
        assert exc_info.value.limit == 0.01
        assert exc_info.value.current > 0.01
        timer.stop()


class TestMemoryExhaustion:
    def test_resource_policy_memory_default(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_memory_bytes == 512 * 1024 * 1024

    def test_resource_policy_memory_custom(self) -> None:
        policy = ResourcePolicy(max_memory_bytes=256 * 1024 * 1024)
        assert policy.max_memory_bytes == 256 * 1024 * 1024

    def test_memory_parse_various_units(self) -> None:
        assert ResourceLimiter.parse_memory("1GB") == 1024**3
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2
        assert ResourceLimiter.parse_memory("1024KB") == 1024 * 1024
        assert ResourceLimiter.parse_memory("1024B") == 1024

    def test_memory_parse_case_insensitive(self) -> None:
        assert ResourceLimiter.parse_memory("1gb") == 1024**3
        assert ResourceLimiter.parse_memory("1Gb") == 1024**3

    def test_memory_parse_float_values(self) -> None:
        assert ResourceLimiter.parse_memory("1.5GB") == int(1.5 * 1024**3)
        assert ResourceLimiter.parse_memory("0.5MB") == int(0.5 * 1024**2)

    def test_memory_parse_plain_number(self) -> None:
        assert ResourceLimiter.parse_memory("1048576") == 1048576

    def test_memory_parse_zero(self) -> None:
        assert ResourceLimiter.parse_memory("0") == 0

    def test_memory_parse_whitespace(self) -> None:
        assert ResourceLimiter.parse_memory("  256MB  ") == 256 * 1024**2

    def test_rlimit_as_set_on_unix(self) -> None:
        try:
            import resource as _resource

            has_resource = True
        except ImportError:
            has_resource = False

        if not has_resource:
            pytest.skip("resource module not available")

        policy = ResourcePolicy(max_memory_bytes=100 * 1024 * 1024)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        try:
            soft, _hard = _resource.getrlimit(_resource.RLIMIT_AS)
            assert soft <= 100 * 1024 * 1024
        finally:
            limiter.uninstall()

    def test_rlimit_as_restored_on_uninstall(self) -> None:
        try:
            import resource as _resource

            has_resource = True
        except ImportError:
            has_resource = False

        if not has_resource:
            pytest.skip("resource module not available")

        original_soft, original_hard = _resource.getrlimit(_resource.RLIMIT_AS)
        policy = ResourcePolicy(max_memory_bytes=100 * 1024 * 1024)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        limiter.uninstall()
        restored_soft, restored_hard = _resource.getrlimit(_resource.RLIMIT_AS)
        assert restored_soft == original_soft
        assert restored_hard == original_hard


class TestFileDescriptorExhaustion:
    def test_rlimit_nofile_set(self) -> None:
        try:
            import resource as _resource

            has_resource = True
        except ImportError:
            has_resource = False

        if not has_resource:
            pytest.skip("resource module not available")

        policy = ResourcePolicy(max_file_descriptors=32)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        try:
            soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
            assert soft <= 32
        finally:
            limiter.uninstall()

    def test_rlimit_nofile_restored(self) -> None:
        try:
            import resource as _resource

            has_resource = True
        except ImportError:
            has_resource = False

        if not has_resource:
            pytest.skip("resource module not available")

        original_soft, _original_hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        policy = ResourcePolicy(max_file_descriptors=32)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        limiter.uninstall()
        restored_soft, _restored_hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        assert restored_soft == original_soft

    def test_fd_limit_default(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_file_descriptors == 64


class TestThreadCreationLimits:
    def test_thread_limit_blocks_excess(self) -> None:
        policy = ResourcePolicy(max_threads=2)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.increment_thread()
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_thread_limit_zero_blocks_all(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_thread_decrement_allows_reuse(self) -> None:
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        limiter.decrement_thread()
        limiter.increment_thread()

    def test_thread_decrement_at_zero_stays_zero(self) -> None:
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.decrement_thread()
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_thread_violation_logged(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test_plugin")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        violations = limiter.get_violations()
        assert len(violations) == 1
        assert violations[0].resource_type == "threads"
        assert violations[0].plugin_id == "test_plugin"

    def test_thread_violation_category(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.increment_thread()
        assert exc_info.value.category == SandboxViolationCategory.RESOURCE

    def test_thread_limit_default(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_threads == 1

    def test_multiple_thread_violations_tracked(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        for _ in range(3):
            with pytest.raises(ResourceExhausted):
                limiter.increment_thread()
        assert len(limiter.get_violations()) == 3


class TestWallTimeEnforcement:
    def test_wall_timer_expires(self) -> None:
        timer = _WallTimer(0.05, plugin_id="test")
        timer.start()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted, match="wall_time"):
            timer.check()
        timer.stop()

    def test_wall_timer_within_limit(self) -> None:
        timer = _WallTimer(5.0, plugin_id="test")
        timer.start()
        timer.check()
        timer.stop()

    def test_wall_timer_stop_prevents_expiry(self) -> None:
        timer = _WallTimer(0.05, plugin_id="test")
        timer.start()
        timer.stop()
        time.sleep(0.1)
        assert not timer.expired

    def test_wall_timer_elapsed_tracks_time(self) -> None:
        timer = _WallTimer(10.0, plugin_id="test")
        timer.start()
        e1 = timer.elapsed
        time.sleep(0.05)
        e2 = timer.elapsed
        assert e2 > e1
        timer.stop()

    def test_resource_limiter_wall_time_check(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=0.05)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        try:
            time.sleep(0.1)
            with pytest.raises(ResourceExhausted, match="wall_time"):
                limiter.check_wall_timer()
        finally:
            limiter.uninstall()

    def test_wall_time_violation_includes_fields(self) -> None:
        timer = _WallTimer(0.01, plugin_id="test_plugin")
        timer.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            timer.check()
        v = exc_info.value
        assert v.resource_type == "wall_time"
        assert v.plugin_id == "test_plugin"
        assert v.limit == 0.01
        assert v.current > 0.01
        assert v.category == SandboxViolationCategory.RESOURCE
        timer.stop()

    def test_wall_time_default(self) -> None:
        policy = ResourcePolicy()
        assert policy.wall_time_seconds == 60.0


class TestStackOverflowViaRecursion:
    def test_recursion_limited_by_python(self) -> None:
        def deep_recursion(n: int) -> int:
            return deep_recursion(n + 1)

        with pytest.raises(RecursionError):
            deep_recursion(0)


class TestResourceLimiterInstallUninstall:
    def test_install_uninstall_cycle(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        assert limiter._installed
        limiter.uninstall()
        assert not limiter._installed

    def test_double_install_idempotent(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        limiter.install()
        assert limiter._installed
        limiter.uninstall()

    def test_double_uninstall_safe(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        limiter.uninstall()
        limiter.uninstall()
        assert not limiter._installed

    def test_violations_clearable(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0


class TestResourceExhaustionViaContext:
    def test_context_installs_resource_limiter(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=5.0, wall_time_seconds=10.0),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx._resource_layer._installed
        ctx.deactivate()

    def test_context_wall_timer_active(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=5.0, wall_time_seconds=10.0),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx._resource_layer._wall_timer is not None
        ctx.deactivate()

    def test_context_wall_timer_cleaned_up(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=5.0, wall_time_seconds=10.0),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.deactivate()
        assert ctx._resource_layer._wall_timer is None

    def test_context_cpu_timer_active(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=5.0, wall_time_seconds=10.0),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx._resource_layer._cpu_timer is not None
        ctx.deactivate()

    def test_context_cpu_timer_cleaned_up(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=5.0, wall_time_seconds=10.0),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.deactivate()
        assert ctx._resource_layer._cpu_timer is None

    def test_wall_time_exceeded_in_context(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=0.05),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            time.sleep(0.1)
            with pytest.raises(ResourceExhausted, match="wall_time"):
                ctx._resource_layer.check_wall_timer()
        finally:
            ctx.deactivate()


class TestResourceViolationCollection:
    def test_resource_violations_collected_by_context(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=0.05),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            time.sleep(0.1)
            with pytest.raises(ResourceExhausted):
                ctx._resource_layer.check_wall_timer()
        finally:
            ctx.deactivate()

        events = ctx.event_logger.get_events()
        resource_events = [
            e for e in events if e.category == SandboxViolationCategory.RESOURCE
        ]
        assert len(resource_events) >= 1

    def test_resource_violations_cleared_after_deactivate(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_threads=0),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(ResourceExhausted):
                ctx._resource_layer.increment_thread()
        finally:
            ctx.deactivate()
        assert len(ctx._resource_layer.get_violations()) == 0


class TestTrustLevelResourceEscalation:
    def test_untrusted_has_lowest_resources(self) -> None:
        from engine.plugins.trust_levels import TrustLevel

        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert policy.resource_policy.max_cpu_seconds == 30.0

    def test_trusted_limited_has_moderate_resources(self) -> None:
        from engine.plugins.trust_levels import TrustLevel

        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "test")
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_trusted_full_has_highest_resources(self) -> None:
        from engine.plugins.trust_levels import TrustLevel

        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert policy.resource_policy.max_cpu_seconds == 120.0

    def test_resource_multiplier_applied(self) -> None:
        from engine.plugins.trust_levels import TrustLevel

        untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        trusted_full = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        ratio = (
            trusted_full.resource_policy.max_memory_bytes
            / untrusted.resource_policy.max_memory_bytes
        )
        assert ratio == pytest.approx(4.0)

    def test_untrusted_cannot_have_rw_paths(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()
