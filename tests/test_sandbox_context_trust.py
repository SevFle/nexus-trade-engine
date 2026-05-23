from __future__ import annotations

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
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.trust_levels import TrustLevel, get_trust_level, get_trust_policy


def _untrusted_policy(
    *,
    blocked_count: int = _MIN_BLOCKED_MODULES_UNTRUSTED,
    cpu: float = _MAX_CPU_SECONDS_UNTRUSTED,
    rw: list[str] | None = None,
) -> SandboxPolicy:
    blocked = {f"mod_{i}" for i in range(blocked_count)}
    return SandboxPolicy(
        trust_level="untrusted",
        import_policy=ImportPolicy(blocked_modules=blocked),
        resource_policy=ResourcePolicy(max_cpu_seconds=cpu),
        filesystem_policy=FilesystemPolicy(read_write_paths=rw or []),
    )


def _limited_policy(
    *,
    blocked_count: int = _MIN_BLOCKED_MODULES_LIMITED,
    cpu: float = _MAX_CPU_SECONDS_LIMITED,
) -> SandboxPolicy:
    blocked = {f"mod_{i}" for i in range(blocked_count)}
    return SandboxPolicy(
        trust_level="trusted_limited",
        import_policy=ImportPolicy(blocked_modules=blocked),
        resource_policy=ResourcePolicy(max_cpu_seconds=cpu),
    )


class TestTrustLevelProperty:
    def test_valid_trust_level(self):
        policy = SandboxPolicy(trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.TRUSTED_FULL

    def test_invalid_trust_level_falls_back(self):
        policy = SandboxPolicy(trust_level="nonsense")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED


class TestValidateTrustLevelUntrusted:
    def test_valid_untrusted(self):
        ctx = SandboxContext(_untrusted_policy())
        assert ctx.validate_trust_level() is True

    def test_untrusted_too_few_blocked(self):
        ctx = SandboxContext(_untrusted_policy(blocked_count=2))
        assert ctx.validate_trust_level() is False

    def test_untrusted_cpu_exceeds(self):
        ctx = SandboxContext(_untrusted_policy(cpu=_MAX_CPU_SECONDS_UNTRUSTED + 1))
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_rw_paths(self):
        ctx = SandboxContext(_untrusted_policy(rw=["/var/data"]))
        assert ctx.validate_trust_level() is False


class TestValidateTrustLevelLimited:
    def test_valid_limited(self):
        ctx = SandboxContext(_limited_policy())
        assert ctx.validate_trust_level() is True

    def test_limited_too_few_blocked(self):
        ctx = SandboxContext(_limited_policy(blocked_count=1))
        assert ctx.validate_trust_level() is False

    def test_limited_cpu_exceeds(self):
        ctx = SandboxContext(_limited_policy(cpu=_MAX_CPU_SECONDS_LIMITED + 1))
        assert ctx.validate_trust_level() is False

    def test_limited_cpu_at_boundary(self):
        ctx = SandboxContext(_limited_policy(cpu=_MAX_CPU_SECONDS_LIMITED))
        assert ctx.validate_trust_level() is True


class TestValidateTrustLevelFull:
    def test_trusted_full_always_valid(self):
        policy = SandboxPolicy(trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestGetTrustLevelFunction:
    def test_valid_manifest(self):
        class M:
            trust_level = "trusted_limited"

        assert get_trust_level(M()) == TrustLevel.TRUSTED_LIMITED

    def test_invalid_manifest(self):
        class M:
            trust_level = "garbage"

        assert get_trust_level(M()) == TrustLevel.UNTRUSTED

    def test_no_trust_level_attr(self):
        assert get_trust_level(object()) == TrustLevel.UNTRUSTED


class TestGetTrustPolicyFunction:
    def test_trusted_full(self):
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["import_restriction"] == "relaxed"

    def test_trusted_limited(self):
        policy = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        assert policy["resource_multiplier"] == 2.0

    def test_untrusted(self):
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["import_restriction"] == "strict"

    def test_unknown_defaults_untrusted(self):
        policy = get_trust_policy("not_a_level")  # type: ignore[arg-type]
        assert policy == get_trust_policy(TrustLevel.UNTRUSTED)
