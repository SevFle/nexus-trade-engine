"""
Layer 2: Network Whitelist — restrict HTTP requests to manifest-declared endpoints.

SandboxedHttpClient wraps httpx.AsyncClient and rejects any request
whose host is not in the strategy manifest's network.allowed_endpoints list.

Production note (Layer 5):
    In production, network isolation should be enforced at the container/VM level.
    This in-process filter is the MVP isolation layer.
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()


class SandboxedHttpClient(httpx.AsyncClient):
    """AsyncClient that only allows requests to whitelisted hosts."""

    def __init__(self, allowed_endpoints: list[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._allowed_endpoints = frozenset(allowed_endpoints)

    async def send(
        self, request: httpx.Request, *, stream: bool = False, **kwargs: object
    ) -> httpx.Response:  # type: ignore[override]
        host = request.url.host
        if not self.is_host_allowed(host):
            logger.warning(
                "sandbox.blocked_network_request",
                host=host,
                allowed=list(self._allowed_endpoints),
            )
            raise PermissionError(f"Network access to '{host}' is not allowed in strategy sandbox")
        return await super().send(request, stream=stream, **kwargs)  # type: ignore[arg-type]

    def is_host_allowed(self, host: str) -> bool:
        return any(host == ep or host.endswith(f".{ep}") for ep in self._allowed_endpoints)
