"""
Layer 2: Network whitelist HTTP client for the strategy sandbox.

Restricts HTTP requests to only the endpoints declared in the strategy
manifest.  Any request to an undeclared host raises ``PermissionError``.
"""

from __future__ import annotations

import httpx


class SandboxedHttpClient(httpx.AsyncClient):
    """HTTP client that only allows requests to whitelisted hosts."""

    def __init__(self, allowed_endpoints: list[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.allowed_endpoints = allowed_endpoints

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        **kwargs: object,
    ) -> httpx.Response:
        host = request.url.host
        if not any(host == ep or host.endswith(f".{ep}") for ep in self.allowed_endpoints):
            raise PermissionError(f"Network access to {host} is not allowed")
        return await super().send(request, stream=stream, **kwargs)  # type: ignore[arg-type]
