from __future__ import annotations

import contextlib
import ipaddress
import socket as _socket_module
from typing import TYPE_CHECKING, Any

import httpx as _httpx_module

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import NetworkPolicy

from engine.plugins.sandbox.core.violation import NetworkViolation

_PRIVATE_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
]

_METADATA_ENDPOINTS: frozenset[str] = frozenset({
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.com",
})


def _is_private_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _parse_cidr_networks(cidrs: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        with contextlib.suppress(ValueError):
            networks.append(ipaddress.ip_network(cidr, strict=False))
    return networks


class NetworkGuard:
    def __init__(self, policy: NetworkPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._violation_log: list[NetworkViolation] = []
        self._original_httpx_send: Any = None
        self._original_socket_create_connection: Any = None
        self._original_getaddrinfo: Any = None
        self._original_socket_class: type | None = None
        self._installed = False
        self._cidr_networks = _parse_cidr_networks(policy.allowed_cidrs)
        self._connection_counts: dict[str, int] = {}

    def _is_host_in_cidr(self, host: str) -> bool:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(addr in net for net in self._cidr_networks)

    def _is_metadata_endpoint(self, host: str) -> bool:
        return host in _METADATA_ENDPOINTS

    def _is_host_allowed(self, host: str) -> bool:
        if self._policy.block_metadata_endpoints and self._is_metadata_endpoint(host):
            return False
        if self._policy.is_host_allowed(host):
            return True
        if _is_private_ip(host) and not self._is_host_in_cidr(host):
            return False
        return self._is_host_in_cidr(host)

    def _is_port_allowed(self, port: int | None) -> bool:
        if port is None:
            return True
        if not self._policy.allowed_ports:
            return True
        return port in self._policy.allowed_ports

    def _make_restricted_send(self, original_send: Any) -> Any:
        guard_ref = self

        async def restricted_send(
            client: Any,
            request: Any,
            *,
            stream: bool = False,
            **kwargs: Any,
        ) -> Any:
            host = request.url.host
            port = request.url.port
            if not guard_ref._is_host_allowed(host):
                violation = NetworkViolation(host, port=port, plugin_id=guard_ref._plugin_id)
                guard_ref._violation_log.append(violation)
                raise PermissionError(violation.detail)
            if port is not None and not guard_ref._is_port_allowed(port):
                violation = NetworkViolation(host, port=port, plugin_id=guard_ref._plugin_id)
                guard_ref._violation_log.append(violation)
                raise PermissionError(
                    f"Connection to {host}:{port} is not allowed (port not whitelisted)"
                )
            return await original_send(client, request, stream=stream, **kwargs)

        return restricted_send

    def _restricted_create_connection(
        self,
        address: tuple[str, int] | tuple[str, int, int, int],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        host = address[0]
        port = address[1]
        if not self._is_host_allowed(host):
            violation = NetworkViolation(host, port=port, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        if not self._is_port_allowed(port):
            violation = NetworkViolation(host, port=port, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(
                f"Connection to {host}:{port} is not allowed (port not whitelisted)"
            )
        return self._original_socket_create_connection(address, *args, **kwargs)

    def _restricted_getaddrinfo(
        self,
        host: str,
        port: int | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        if self._policy.block_dns and not self._is_host_allowed(host):
            violation = NetworkViolation(host, port=port, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(f"DNS lookup for {host} is not allowed")
        return self._original_getaddrinfo(host, port, *args, **kwargs)

    def install(self) -> None:
        if self._installed:
            return
        self._original_httpx_send = _httpx_module.AsyncClient.send
        _httpx_module.AsyncClient.send = self._make_restricted_send(self._original_httpx_send)

        self._original_socket_create_connection = _socket_module.create_connection
        _socket_module.create_connection = self._restricted_create_connection

        self._original_getaddrinfo = _socket_module.getaddrinfo
        _socket_module.getaddrinfo = self._restricted_getaddrinfo

        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        if self._original_httpx_send is not None:
            _httpx_module.AsyncClient.send = self._original_httpx_send
            self._original_httpx_send = None
        if self._original_socket_create_connection is not None:
            _socket_module.create_connection = self._original_socket_create_connection
            self._original_socket_create_connection = None
        if self._original_getaddrinfo is not None:
            _socket_module.getaddrinfo = self._original_getaddrinfo
            self._original_getaddrinfo = None
        self._installed = False

    def get_violations(self) -> list[NetworkViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
