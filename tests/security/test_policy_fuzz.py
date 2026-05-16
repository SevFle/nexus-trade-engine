"""
Fuzz testing for sandbox policy configuration parsing.

Uses property-based testing (hypothesis) to verify that policy
parsing and construction is robust against arbitrary input,
including malformed, adversarial, and edge-case configurations.

Tests cover:
  - SandboxPolicy construction from arbitrary values
  - ImportPolicy with various module name inputs
  - NetworkPolicy with various endpoint/CIDR inputs
  - ResourcePolicy with boundary and negative values
  - FilesystemPolicy with various path inputs
  - IntrospectionPolicy with various builtin/attribute names
  - Memory string parsing with arbitrary strings
  - TrustLevel resolution with arbitrary strings
  - Combined policy validation
"""

from __future__ import annotations

import string

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import (
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
from engine.plugins.trust_levels import TrustLevel, get_trust_level

_module_name_chars = st.text(
    st.sampled_from(string.ascii_letters + string.digits + "._-"),
    min_size=1,
    max_size=80,
)

_path_chars = st.text(
    st.sampled_from(string.printable.replace("\x00", "")),
    min_size=1,
    max_size=200,
)


class TestImportPolicyFuzz:
    @given(
        blocked=st.sets(_module_name_chars, max_size=20),
        allowed=st.sets(_module_name_chars, max_size=20),
    )
    @settings(max_examples=50)
    def test_is_allowed_never_crashes(
        self,
        blocked: set[str],
        allowed: set[str],
    ) -> None:
        policy = ImportPolicy(blocked_modules=blocked, allowed_modules=allowed)
        result = policy.is_allowed("test_module")
        assert isinstance(result, bool)

    @given(module=_module_name_chars)
    @settings(max_examples=50)
    def test_is_allowed_single_module(self, module: str) -> None:
        root = module.split(".", maxsplit=1)[0]
        assume(root and root == module)
        policy = ImportPolicy(blocked_modules={module})
        assert not policy.is_allowed(module)

    @given(
        module=_module_name_chars,
        blocked=st.sets(_module_name_chars, max_size=10),
    )
    @settings(max_examples=50)
    def test_blocked_overrides_allowed(self, module: str, blocked: set[str]) -> None:
        root = module.split(".", maxsplit=1)[0]
        assume(root and root == module)
        policy = ImportPolicy(
            allowed_modules={module},
            blocked_modules=blocked | {module},
        )
        assert not policy.is_allowed(module)

    @given(module=_module_name_chars)
    @settings(max_examples=50)
    def test_empty_blocked_allows_all(self, module: str) -> None:
        assume(module.split(".", maxsplit=1)[0])
        policy = ImportPolicy(blocked_modules=set(), allowed_modules=None)
        assert policy.is_allowed(module)

    @given(module=_module_name_chars)
    @settings(max_examples=50)
    def test_submodule_blocked_by_root(self, module: str) -> None:
        assume(module.split(".", maxsplit=1)[0])
        root = module.split(".", maxsplit=1)[0] if "." in module else module
        policy = ImportPolicy(blocked_modules={root})
        assert not policy.is_allowed(module + ".submodule")


class TestNetworkPolicyFuzz:
    @given(
        endpoints=st.lists(st.text(min_size=1, max_size=100), max_size=10),
    )
    @settings(max_examples=50)
    def test_is_host_allowed_never_crashes(
        self,
        endpoints: list[str],
    ) -> None:
        policy = NetworkPolicy(allowed_endpoints=endpoints)
        result = policy.is_host_allowed("test.example.com")
        assert isinstance(result, bool)

    @given(
        host=st.text(min_size=1, max_size=100),
        endpoints=st.lists(st.text(min_size=1, max_size=100), max_size=5),
    )
    @settings(max_examples=50)
    def test_host_check_never_crashes(self, host: str, endpoints: list[str]) -> None:
        policy = NetworkPolicy(allowed_endpoints=endpoints)
        result = policy.is_host_allowed(host)
        assert isinstance(result, bool)

    @given(endpoint=st.text(min_size=1, max_size=50))
    @settings(max_examples=50)
    def test_exact_match(self, endpoint: str) -> None:
        policy = NetworkPolicy(allowed_endpoints=[endpoint])
        assert policy.is_host_allowed(endpoint)

    def test_empty_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        assert not policy.is_host_allowed("any.host.com")

    @given(
        cidrs=st.lists(
            st.one_of(
                st.just("10.0.0.0/8"),
                st.just("192.168.0.0/16"),
                st.just("::1/128"),
                st.just("invalid-cidr"),
                st.text(min_size=1, max_size=30),
            ),
            max_size=5,
        ),
    )
    @settings(max_examples=50)
    def test_cidr_parsing_never_crashes(self, cidrs: list[str]) -> None:
        policy = NetworkPolicy(allowed_cidrs=cidrs)
        assert isinstance(policy.allowed_cidrs, list)

    @given(
        ports=st.sets(st.integers(min_value=1, max_value=65535), max_size=10),
    )
    @settings(max_examples=50)
    def test_allowed_ports_never_crashes(self, ports: set[int]) -> None:
        policy = NetworkPolicy(allowed_ports=ports)
        assert isinstance(policy.allowed_ports, set)


class TestResourcePolicyFuzz:
    @given(
        cpu=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        memory=st.integers(min_value=0, max_value=2**63),
        fds=st.integers(min_value=0, max_value=10000),
        threads=st.integers(min_value=0, max_value=1000),
        wall=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=50)
    def test_construction_never_crashes(
        self,
        cpu: float,
        memory: int,
        fds: int,
        threads: int,
        wall: float,
    ) -> None:
        policy = ResourcePolicy(
            max_cpu_seconds=cpu,
            max_memory_bytes=memory,
            max_file_descriptors=fds,
            max_threads=threads,
            wall_time_seconds=wall,
        )
        assert policy.max_cpu_seconds == cpu
        assert policy.max_memory_bytes == memory
        assert policy.max_file_descriptors == fds
        assert policy.max_threads == threads
        assert policy.wall_time_seconds == wall


class TestFilesystemPolicyFuzz:
    @given(
        ro_paths=st.lists(_path_chars, max_size=10),
        rw_paths=st.lists(_path_chars, max_size=10),
        block_symlinks=st.booleans(),
        block_absolute=st.booleans(),
    )
    @settings(max_examples=50)
    def test_construction_never_crashes(
        self,
        ro_paths: list[str],
        rw_paths: list[str],
        block_symlinks: bool,
        block_absolute: bool,
    ) -> None:
        policy = FilesystemPolicy(
            read_only_paths=ro_paths,
            read_write_paths=rw_paths,
            block_symlinks=block_symlinks,
            block_absolute_paths=block_absolute,
        )
        assert policy.read_only_paths == ro_paths
        assert policy.read_write_paths == rw_paths


class TestIntrospectionPolicyFuzz:
    @given(
        builtins=st.sets(
            st.text(min_size=1, max_size=30, alphabet=string.ascii_letters + "_"),
            max_size=20,
        ),
        attrs=st.sets(
            st.text(min_size=1, max_size=30, alphabet=string.ascii_letters + "_"),
            max_size=20,
        ),
    )
    @settings(max_examples=50)
    def test_construction_never_crashes(
        self,
        builtins: set[str],
        attrs: set[str],
    ) -> None:
        policy = IntrospectionPolicy(
            blocked_builtins=builtins,
            blocked_attributes=attrs,
        )
        assert policy.blocked_builtins == builtins
        assert policy.blocked_attributes == attrs


class TestSandboxPolicyFuzz:
    @given(
        plugin_id=st.text(min_size=1, max_size=50),
        trust_level=st.sampled_from(["untrusted", "trusted_limited", "trusted_full"]),
    )
    @settings(max_examples=30)
    def test_from_trust_level_never_crashes(
        self,
        plugin_id: str,
        trust_level: str,
    ) -> None:
        level = TrustLevel(trust_level)
        policy = SandboxPolicy.from_trust_level(level, plugin_id)
        assert policy.plugin_id == plugin_id
        assert policy.trust_level == trust_level

    @given(
        plugin_id=st.text(min_size=0, max_size=100),
        trust_level=st.text(min_size=0, max_size=50),
    )
    @settings(max_examples=50)
    def test_direct_construction_never_crashes(
        self,
        plugin_id: str,
        trust_level: str,
    ) -> None:
        policy = SandboxPolicy(plugin_id=plugin_id, trust_level=trust_level)
        assert policy.plugin_id == plugin_id
        assert policy.trust_level == trust_level


class TestMemoryStringFuzz:
    @given(s=st.text(min_size=1, max_size=30))
    @settings(max_examples=200)
    def test_parse_memory_never_crashes(self, s: str) -> None:
        try:
            result = _parse_memory(s)
            assert isinstance(result, int)
            assert result >= 0
        except (ValueError, IndexError):
            pass

    @given(s=st.text(min_size=1, max_size=30))
    @settings(max_examples=200)
    def test_resource_limiter_parse_memory_never_crashes(self, s: str) -> None:
        try:
            result = ResourceLimiter.parse_memory(s)
            assert isinstance(result, int)
        except (ValueError, IndexError):
            pass

    def test_valid_units(self) -> None:
        assert _parse_memory("1B") == 1
        assert _parse_memory("1KB") == 1024
        assert _parse_memory("1MB") == 1024**2
        assert _parse_memory("1GB") == 1024**3

    def test_case_insensitive(self) -> None:
        assert _parse_memory("1mb") == _parse_memory("1MB")
        assert _parse_memory("1gb") == _parse_memory("1GB")

    def test_float_values(self) -> None:
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)
        assert _parse_memory("0.5MB") == int(0.5 * 1024**2)

    def test_whitespace_handling(self) -> None:
        assert _parse_memory("  256MB  ") == 256 * 1024**2

    def test_plain_number(self) -> None:
        assert _parse_memory("1048576") == 1048576


class TestTrustLevelFuzz:
    @given(trust_str=st.text(min_size=0, max_size=50))
    @settings(max_examples=100)
    def test_get_trust_level_never_crashes(self, trust_str: str) -> None:
        result = get_trust_level(type("FakeManifest", (), {"trust_level": trust_str})())
        assert isinstance(result, TrustLevel)

    @given(trust_str=st.text(min_size=0, max_size=50))
    @settings(max_examples=100)
    def test_invalid_trust_defaults_to_untrusted(self, trust_str: str) -> None:
        valid_values = {"untrusted", "trusted_limited", "trusted_full"}
        if trust_str not in valid_values:
            result = get_trust_level(type("FakeManifest", (), {"trust_level": trust_str})())
            assert result == TrustLevel.UNTRUSTED

    def test_valid_trust_levels(self) -> None:
        assert get_trust_level(
            type("M", (), {"trust_level": "untrusted"})()
        ) == TrustLevel.UNTRUSTED
        assert get_trust_level(
            type("M", (), {"trust_level": "trusted_limited"})()
        ) == TrustLevel.TRUSTED_LIMITED
        assert get_trust_level(
            type("M", (), {"trust_level": "trusted_full"})()
        ) == TrustLevel.TRUSTED_FULL

    def test_none_trust_defaults_untrusted(self) -> None:
        assert get_trust_level(type("M", (), {"trust_level": None})()) == TrustLevel.UNTRUSTED

    def test_missing_trust_defaults_untrusted(self) -> None:
        assert get_trust_level(type("M", (), {})()) == TrustLevel.UNTRUSTED


class TestViolationCategoryFuzz:
    def test_all_categories_have_values(self) -> None:
        categories = list(SandboxViolationCategory)
        values = [c.value for c in categories]
        assert "import" in values
        assert "network" in values
        assert "resource" in values
        assert "filesystem" in values
        assert "introspection" in values

    @given(value=st.text(min_size=1, max_size=30))
    @settings(max_examples=50)
    def test_invalid_category_raises(self, value: str) -> None:
        valid_values = {c.value for c in SandboxViolationCategory}
        if value not in valid_values:
            with pytest.raises(ValueError):
                SandboxViolationCategory(value)

    def test_category_from_value(self) -> None:
        assert SandboxViolationCategory("import") == SandboxViolationCategory.IMPORT
        assert SandboxViolationCategory("network") == SandboxViolationCategory.NETWORK
        assert SandboxViolationCategory("resource") == SandboxViolationCategory.RESOURCE
        assert SandboxViolationCategory("filesystem") == SandboxViolationCategory.FILESYSTEM
        assert SandboxViolationCategory("introspection") == SandboxViolationCategory.INTROSPECTION
