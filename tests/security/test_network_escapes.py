from __future__ import annotations

import socket
from unittest.mock import MagicMock

import httpx
import pytest

from engine.plugins.sandbox.core.policy import NetworkPolicy
from engine.plugins.sandbox.layers.network_guard import NetworkGuard


@pytest.fixture
def strict_policy() -> NetworkPolicy:
    return NetworkPolicy(
        allowed_endpoints=["api.example.com"],
        allowed_cidrs=[],
        allowed_ports={443},
        block_dns=True,
    )


@pytest.fixture
def cidr_policy() -> NetworkPolicy:
    return NetworkPolicy(
        allowed_endpoints=[],
        allowed_cidrs=["10.0.0.0/8", "192.168.0.0/16"],
        allowed_ports=set(),
        block_dns=False,
    )


@pytest.fixture
def permissive_policy() -> NetworkPolicy:
    return NetworkPolicy(
        allowed_endpoints=["example.com"],
        allowed_cidrs=[],
        allowed_ports=set(),
        block_dns=False,
    )


class TestHostWhitelist:
    def test_exact_host_match(self, strict_policy: NetworkPolicy) -> None:
        assert strict_policy.is_host_allowed("api.example.com") is True

    def test_subdomain_match(self, strict_policy: NetworkPolicy) -> None:
        assert strict_policy.is_host_allowed("sub.api.example.com") is True

    def test_unrelated_host_blocked(self, strict_policy: NetworkPolicy) -> None:
        assert strict_policy.is_host_allowed("evil.com") is False

    def test_partial_name_not_matched(self, strict_policy: NetworkPolicy) -> None:
        assert strict_policy.is_host_allowed("notexample.com") is False

    def test_empty_whitelist_blocks_all(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        assert policy.is_host_allowed("anything.com") is False


class TestCIDRMatching:
    def test_ip_in_cidr_range(self, cidr_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(cidr_policy)
        assert guard._is_host_in_cidr("10.0.1.50") is True

    def test_ip_in_second_cidr_range(self, cidr_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(cidr_policy)
        assert guard._is_host_in_cidr("192.168.1.1") is True

    def test_ip_outside_cidr_range(self, cidr_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(cidr_policy)
        assert guard._is_host_in_cidr("8.8.8.8") is False

    def test_broadcast_address(self, cidr_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(cidr_policy)
        assert guard._is_host_in_cidr("10.255.255.255") is True

    def test_network_address(self, cidr_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(cidr_policy)
        assert guard._is_host_in_cidr("10.0.0.0") is True

    def test_invalid_ip_returns_false(self, cidr_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(cidr_policy)
        assert guard._is_host_in_cidr("not_an_ip") is False

    def test_cidr_combined_with_host_whitelist(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["safe.com"],
            allowed_cidrs=["172.16.0.0/12"],
        )
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("safe.com") is True
        assert guard._is_host_allowed("172.16.5.5") is True
        assert guard._is_host_allowed("evil.com") is False
        assert guard._is_host_allowed("8.8.8.8") is False


class TestSocketCreateConnection:
    def test_blocked_host_raises(self, strict_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(strict_policy)
        guard._original_socket_create_connection = socket.create_connection
        with pytest.raises(PermissionError, match="not allowed"):
            guard._restricted_create_connection(("evil.com", 443))

    def test_violation_logged_on_blocked_host(self, strict_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(strict_policy, plugin_id="test_plugin")
        guard._original_socket_create_connection = socket.create_connection
        with pytest.raises(PermissionError):
            guard._restricted_create_connection(("evil.com", 80))
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].host == "evil.com"
        assert violations[0].port == 80


class TestDNSInterception:
    def test_dns_blocked_for_non_whitelisted(self, strict_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(strict_policy)
        guard._original_getaddrinfo = socket.getaddrinfo
        with pytest.raises(PermissionError, match="DNS lookup"):
            guard._restricted_getaddrinfo("evil.com", 443)


class TestHttpxMonkeyPatch:
    async def test_blocked_host_via_httpx_send(self, strict_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(strict_policy, plugin_id="test_plugin")
        original = httpx.AsyncClient.send
        guard._original_httpx_send = original
        restricted = guard._make_restricted_send(original)

        client = MagicMock()
        request = httpx.Request("GET", "https://evil.com/api")
        with pytest.raises(PermissionError, match="not allowed"):
            await restricted(client, request)

        violations = guard.get_violations()
        assert len(violations) == 1


class TestNetworkGuardLifecycle:
    def test_install_and_uninstall(self, strict_policy: NetworkPolicy) -> None:
        original_create = socket.create_connection
        original_getaddr = socket.getaddrinfo
        original_send = httpx.AsyncClient.send

        guard = NetworkGuard(strict_policy)
        guard.install()

        assert socket.create_connection is not original_create
        assert socket.getaddrinfo is not original_getaddr
        assert httpx.AsyncClient.send is not original_send

        guard.uninstall()

        assert socket.create_connection is original_create
        assert socket.getaddrinfo is original_getaddr
        assert httpx.AsyncClient.send is original_send

    def test_double_install_safe(self, strict_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(strict_policy)
        guard.install()
        guard.install()
        guard.uninstall()
        guard.uninstall()

    def test_violations_cleared(self, strict_policy: NetworkPolicy) -> None:
        guard = NetworkGuard(strict_policy, plugin_id="test")
        guard._original_socket_create_connection = socket.create_connection
        with pytest.raises(PermissionError):
            guard._restricted_create_connection(("evil.com", 80))
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0
