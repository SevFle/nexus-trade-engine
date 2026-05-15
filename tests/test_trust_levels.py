"""Comprehensive tests for engine.plugins.trust_levels."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.plugins.trust_levels import (
    TrustLevel,
    get_trust_level,
    get_trust_policy,
)


class TestTrustLevelEnum:
    def test_trusted_full_value(self) -> None:
        assert TrustLevel.TRUSTED_FULL.value == "trusted_full"

    def test_trusted_limited_value(self) -> None:
        assert TrustLevel.TRUSTED_LIMITED.value == "trusted_limited"

    def test_untrusted_value(self) -> None:
        assert TrustLevel.UNTRUSTED.value == "untrusted"

    def test_from_string_trusted_full(self) -> None:
        assert TrustLevel("trusted_full") is TrustLevel.TRUSTED_FULL

    def test_from_string_trusted_limited(self) -> None:
        assert TrustLevel("trusted_limited") is TrustLevel.TRUSTED_LIMITED

    def test_from_string_untrusted(self) -> None:
        assert TrustLevel("untrusted") is TrustLevel.UNTRUSTED

    def test_invalid_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            TrustLevel("invalid_level")

    def test_all_members(self) -> None:
        members = list(TrustLevel)
        assert len(members) == 3


class TestGetTrustLevel:
    def test_manifest_with_trusted_full(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_full")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_FULL

    def test_manifest_with_trusted_limited(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_limited")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_LIMITED

    def test_manifest_with_untrusted(self) -> None:
        manifest = SimpleNamespace(trust_level="untrusted")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_none_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level=None)
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_without_trust_level_attr(self) -> None:
        manifest = SimpleNamespace()
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_empty_string(self) -> None:
        manifest = SimpleNamespace(trust_level="")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_invalid_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level="super_admin")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED


class TestGetTrustPolicy:
    def test_trusted_full_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["import_restriction"] == "relaxed"
        assert policy["network"] == "manifest_only"
        assert policy["resource_multiplier"] == 4.0
        assert policy["filesystem"] == "workspace"
        assert policy["introspection"] == "basic"

    def test_trusted_limited_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        assert policy["import_restriction"] == "standard"
        assert policy["network"] == "manifest_only"
        assert policy["resource_multiplier"] == 2.0
        assert policy["filesystem"] == "isolated_rw"
        assert policy["introspection"] == "standard"

    def test_untrusted_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["import_restriction"] == "strict"
        assert policy["network"] == "manifest_only"
        assert policy["resource_multiplier"] == 1.0
        assert policy["filesystem"] == "isolated_ro"
        assert policy["introspection"] == "strict"

    def test_returns_dict(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert isinstance(policy, dict)

    def test_all_policies_have_required_keys(self) -> None:
        required_keys = {
            "import_restriction",
            "network",
            "resource_multiplier",
            "filesystem",
            "introspection",
        }
        for level in TrustLevel:
            policy = get_trust_policy(level)
            assert required_keys.issubset(policy.keys())

    def test_resource_multiplier_decreases_with_trust(self) -> None:
        full = get_trust_policy(TrustLevel.TRUSTED_FULL)["resource_multiplier"]
        limited = get_trust_policy(TrustLevel.TRUSTED_LIMITED)["resource_multiplier"]
        untrusted = get_trust_policy(TrustLevel.UNTRUSTED)["resource_multiplier"]
        assert full > limited > untrusted
