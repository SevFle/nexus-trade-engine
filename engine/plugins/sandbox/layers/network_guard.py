from __future__ import annotations

import ipaddress
import socket as _socket_module
from typing import TYPE_CHECKING, Any

import httpx as _httpx_module

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import NetworkPolicy

from engine.plugins.sandbox.core.violation import NetworkViolation


def _is_private_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        return False
    else:
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved


class NetworkGuard:
    def __init__(self, policy: NetworkPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._violation_log: list[NetworkViolation] = []
        self._original_httpx_send: Any = None
        self._original_socket_create_connection: Any = None
        self._original_getaddrinfo: Any = None
        self._installed = False

    def _is_port_allowed(self, port: int | None) -> bool:
        if port is None:
            return True
        if not self._policy.allowed_ports:
            return True
        return port in self._policy.allowed_ports

    def _is_host_in_cidr(self, host: str) -> bool:
        for cidr in self._policy.allowed_cidrs:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
                addr = ipaddress.ip_address(host)
                if addr in network:
                    return True
            except (ValueError, TypeError):
                continue
        return False

    def _is_host_allowed(self, host: str) -> bool:
        if (
            _is_private_ip(host)
            and not self._is_host_in_cidr(host)
            and not self._policy.is_host_allowed(host)
        ):
            return False
        return self._policy.is_host_allowed(host) or self._is_host_in_cidr(host)

    def _make_restricted_send(self, original_send: Any) -> Any:
        policy = self._policy
        plugin_id = self._plugin_id

        async def restricted_send(
            client: Any,
            request: Any,
            *,
            stream: bool = False,
            **kwargs: Any,
        ) -> Any:
            host = request.url.host
            if not policy.is_host_allowed(host):
                violation = NetworkViolation(host, plugin_id=plugin_id)
                self._violation_log.append(violation)
                raise PermissionError(violation.detail)
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
        if not self._policy.is_host_allowed(host):
            violation = NetworkViolation(host, port=port, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        if not self._is_port_allowed(port):
            violation = NetworkViolation(host, port=port, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(
                f"Network access port not whitelisted: {port} for {host} in strategy sandbox"
            )
        return self._original_socket_create_connection(address, *args, **kwargs)

    def _restricted_getaddrinfo(
        self,
        host: str,
        port: int | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        if self._policy.block_dns and not self._policy.is_host_allowed(host):
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
