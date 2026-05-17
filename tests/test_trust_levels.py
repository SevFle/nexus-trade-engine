"""Tests for trust level enforcement: capabilities, escalation, and validation."""

from __future__ import annotations

from types import SimpleNamespace

from engine.plugins.trust_levels import (
    TrustLevel,
    enforce_no_escalation,
    get_allowed_capabilities,
    get_trust_level,
    get_trust_policy,
    validate_capabilities,
)


class TestTrustLevelEnum:
    def test_values(self) -> None:
        assert TrustLevel.TRUSTED_FULL.value == "trusted_full"
        assert TrustLevel.TRUSTED_LIMITED.value == "trusted_limited"
        assert TrustLevel.UNTRUSTED.value == "untrusted"

    def test_from_value(self) -> None:
        assert TrustLevel("trusted_full") is TrustLevel.TRUSTED_FULL
        assert TrustLevel("trusted_limited") is TrustLevel.TRUSTED_LIMITED
        assert TrustLevel("untrusted") is TrustLevel.UNTRUSTED

    def test_invalid_value_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            TrustLevel("super_admin")


class TestGetTrustLevel:
    def test_manifest_with_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_full")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_FULL

    def test_manifest_without_trust_level(self) -> None:
        manifest = SimpleNamespace()
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_none_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level=None)
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_empty_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level="")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_invalid_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level="invalid")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED


class TestGetTrustPolicy:
    def test_all_levels_have_required_keys(self) -> None:
        for level in TrustLevel:
            policy = get_trust_policy(level)
            assert "import_restriction" in policy
            assert "resource_multiplier" in policy
            assert "filesystem" in policy
            assert "introspection" in policy
            assert "allowed_capabilities" in policy

    def test_unknown_level_returns_untrusted(self) -> None:
        policy = get_trust_policy("unknown")  # type: ignore[arg-type]
        assert policy["import_restriction"] == "strict"

    def test_resource_multiplier_ordering(self) -> None:
        p_untrusted = get_trust_policy(TrustLevel.UNTRUSTED)
        p_limited = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        p_full = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert p_untrusted["resource_multiplier"] < p_limited["resource_multiplier"]
        assert p_limited["resource_multiplier"] < p_full["resource_multiplier"]


class TestGetAllowedCapabilities:
    def test_untrusted_minimal_capabilities(self) -> None:
        caps = get_allowed_capabilities(TrustLevel.UNTRUSTED)
        assert "network" in caps
        assert "filesystem_read" in caps
        assert "filesystem_write" not in caps
        assert "subprocess" not in caps
        assert "threads" not in caps

    def test_limited_additional_capabilities(self) -> None:
        caps = get_allowed_capabilities(TrustLevel.TRUSTED_LIMITED)
        assert "filesystem_write" in caps
        assert "threads" in caps
        assert "subprocess" not in caps

    def test_full_all_capabilities(self) -> None:
        caps = get_allowed_capabilities(TrustLevel.TRUSTED_FULL)
        assert "subprocess" in caps
        assert "environment" in caps
        assert "dynamic_import" in caps

    def test_capability_ordering(self) -> None:
        untrusted = get_allowed_capabilities(TrustLevel.UNTRUSTED)
        limited = get_allowed_capabilities(TrustLevel.TRUSTED_LIMITED)
        full = get_allowed_capabilities(TrustLevel.TRUSTED_FULL)
        assert untrusted < limited
        assert limited < full


class TestValidateCapabilities:
    def test_untrusted_allows_network(self) -> None:
        assert validate_capabilities(TrustLevel.UNTRUSTED, {"network"}) is True

    def test_untrusted_blocks_write(self) -> None:
        assert validate_capabilities(TrustLevel.UNTRUSTED, {"filesystem_write"}) is False

    def test_untrusted_blocks_threads(self) -> None:
        assert validate_capabilities(TrustLevel.UNTRUSTED, {"threads"}) is False

    def test_limited_allows_write(self) -> None:
        assert validate_capabilities(TrustLevel.TRUSTED_LIMITED, {"filesystem_write"}) is True

    def test_limited_blocks_subprocess(self) -> None:
        assert validate_capabilities(TrustLevel.TRUSTED_LIMITED, {"subprocess"}) is False

    def test_full_allows_subprocess(self) -> None:
        assert validate_capabilities(TrustLevel.TRUSTED_FULL, {"subprocess"}) is True

    def test_empty_required_always_passes(self) -> None:
        assert validate_capabilities(TrustLevel.UNTRUSTED, set()) is True

    def test_multiple_capabilities(self) -> None:
        assert (
            validate_capabilities(
                TrustLevel.UNTRUSTED, {"network", "filesystem_read"}
            )
            is True
        )

    def test_mixed_pass_fail(self) -> None:
        assert (
            validate_capabilities(
                TrustLevel.UNTRUSTED, {"network", "filesystem_write"}
            )
            is False
        )


class TestEnforceNoEscalation:
    def test_same_level_allowed(self) -> None:
        assert enforce_no_escalation(TrustLevel.UNTRUSTED, TrustLevel.UNTRUSTED) is True

    def test_downgrade_allowed(self) -> None:
        assert enforce_no_escalation(TrustLevel.TRUSTED_FULL, TrustLevel.UNTRUSTED) is True

    def test_upgrade_blocked(self) -> None:
        assert enforce_no_escalation(TrustLevel.UNTRUSTED, TrustLevel.TRUSTED_FULL) is False

    def test_limited_to_full_blocked(self) -> None:
        assert enforce_no_escalation(TrustLevel.TRUSTED_LIMITED, TrustLevel.TRUSTED_FULL) is False

    def test_untrusted_to_limited_blocked(self) -> None:
        assert enforce_no_escalation(TrustLevel.UNTRUSTED, TrustLevel.TRUSTED_LIMITED) is False

    def test_full_to_limited_allowed(self) -> None:
        assert enforce_no_escalation(TrustLevel.TRUSTED_FULL, TrustLevel.TRUSTED_LIMITED) is True

    def test_limited_to_untrusted_allowed(self) -> None:
        assert enforce_no_escalation(TrustLevel.TRUSTED_LIMITED, TrustLevel.UNTRUSTED) is True
