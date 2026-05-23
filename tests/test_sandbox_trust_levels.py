from __future__ import annotations

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.trust_levels import TrustLevel


def _untrusted_policy(**overrides) -> SandboxPolicy:
    defaults = {
        "plugin_id": "test",
        "trust_level": "untrusted",
        "import_policy": ImportPolicy(
            blocked_modules={
                "os", "subprocess", "shutil", "pathlib", "io", "_io",
                "socket", "_socket", "http", "urllib", "ftplib", "smtplib",
                "ctypes", "_ctypes", "multiprocessing", "signal", "sys",
                "importlib", "threading", "_thread", "concurrent",
            },
        ),
        "resource_policy": ResourcePolicy(max_cpu_seconds=30),
        "filesystem_policy": FilesystemPolicy(read_write_paths=[]),
    }
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


def _limited_policy(**overrides) -> SandboxPolicy:
    defaults = {
        "plugin_id": "test",
        "trust_level": "trusted_limited",
        "import_policy": ImportPolicy(
            blocked_modules={"os", "subprocess", "shutil", "ctypes", "_ctypes"},
        ),
        "resource_policy": ResourcePolicy(max_cpu_seconds=60),
        "filesystem_policy": FilesystemPolicy(read_write_paths=[]),
    }
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


class TestTrustLevelProperty:
    def test_returns_raw_string(self):
        policy = _untrusted_policy()
        ctx = SandboxContext(policy)
        assert ctx.trust_level == "untrusted"

    def test_returns_limited_string(self):
        policy = _limited_policy()
        ctx = SandboxContext(policy)
        assert ctx.trust_level == "trusted_limited"


class TestValidateTrustLevel:
    def test_untrusted_valid(self):
        policy = _untrusted_policy()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_untrusted_too_few_blocked_modules(self):
        policy = _untrusted_policy(
            import_policy=ImportPolicy(
                blocked_modules={"os", "subprocess"},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_cpu_too_high(self):
        policy = _untrusted_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_has_rw_paths(self):
        policy = _untrusted_policy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/nope"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_valid(self):
        policy = _limited_policy()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_limited_too_few_blocked_modules(self):
        policy = _limited_policy(
            import_policy=ImportPolicy(
                blocked_modules={"os"},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_cpu_too_high(self):
        policy = _limited_policy(
            resource_policy=ResourcePolicy(max_cpu_seconds=200),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_trusted_full_always_valid(self):
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_invalid_string_treated_as_untrusted(self):
        policy = _untrusted_policy(trust_level="nonsense")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_invalid_string_with_bad_config(self):
        policy = _untrusted_policy(
            trust_level="nonsense",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False


class TestPolicyFromTrustLevel:
    def test_untrusted_string(self):
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="p1")
        assert policy.trust_level == "untrusted"
        assert policy.plugin_id == "p1"
        assert len(policy.import_policy.blocked_modules) > 10

    def test_limited_string(self):
        policy = SandboxPolicy.from_trust_level("trusted_limited", plugin_id="p2")
        assert policy.trust_level == "trusted_limited"
        assert policy.resource_policy.max_cpu_seconds == 120

    def test_full_string(self):
        policy = SandboxPolicy.from_trust_level("trusted_full", plugin_id="p3")
        assert policy.trust_level == "trusted"
        assert policy.resource_policy.max_cpu_seconds == 300

    def test_enum_input(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED)
        assert policy.trust_level == "untrusted"

    def test_invalid_string_defaults_to_untrusted(self):
        policy = SandboxPolicy.from_trust_level("invalid_value")
        assert policy.trust_level == "untrusted"

    def test_limited_enum(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED)
        assert policy.trust_level == "trusted_limited"
