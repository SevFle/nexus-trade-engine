from __future__ import annotations

import contextlib
import ipaddress
import socket as _socket_module
from typing import TYPE_CHECKING, Any

from engine.plugins.sandbox.core.violation import NetworkViolation

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import NetworkPolicy


class DnsGuard:
    def __init__(self, policy: NetworkPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._original_getaddrinfo: Any = None
        self._violation_log: list[NetworkViolation] = []

    def _is_host_allowed(self, host: str) -> bool:
        return self._policy.is_host_allowed(host) or self._is_host_in_cidr(host)

    def _is_host_in_cidr(self, host: str) -> bool:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        with contextlib.suppress(ValueError):
            for cidr in self._policy.allowed_cidrs:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
        return False

    def restricted_getaddrinfo(
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

    def install(self, original_getaddrinfo: Any) -> None:
        self._original_getaddrinfo = original_getaddrinfo
        _socket_module.getaddrinfo = self.restricted_getaddrinfo

    def uninstall(self) -> None:
        if self._original_getaddrinfo is not None:
            _socket_module.getaddrinfo = self._original_getaddrinfo
            self._original_getaddrinfo = None

    def get_violations(self) -> list[NetworkViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
