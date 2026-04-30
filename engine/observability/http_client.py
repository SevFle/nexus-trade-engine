"""httpx integration that propagates correlation id on every outbound call.

Use :func:`correlated_async_client` instead of constructing
``httpx.AsyncClient`` directly so that any tail-end service receives the
``X-Correlation-Id`` header that ties its logs back to the originating
request.

By default the header is injected on every request. Pass
``trusted_hosts={"internal-svc-a", "internal-svc-b"}`` to restrict
injection to known internal destinations and avoid leaking internal ids
to third-party vendors / brokers.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from engine.observability import context as ctx
from engine.observability.middleware import CORRELATION_HEADER


def correlation_id_request_hook(
    request: httpx.Request, trusted_hosts: frozenset[str] | None = None
) -> None:
    """Attach the bound correlation id if missing and the host is trusted."""
    if request.headers.get(CORRELATION_HEADER):
        return
    if trusted_hosts is not None:
        host = request.url.host
        if host not in trusted_hosts:
            return
    cid = ctx.get_correlation_id()
    if cid:
        request.headers[CORRELATION_HEADER] = cid


def correlated_async_client(
    *, trusted_hosts: set[str] | frozenset[str] | None = None, **kwargs: Any
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` with the correlation hook attached.

    ``trusted_hosts`` — when provided, only requests targeting these hosts
    receive the ``X-Correlation-Id`` header. Recommended for any client
    that may call external services. Pass ``None`` (default) to inject on
    every request.
    """
    frozen: frozenset[str] | None = (
        frozenset(trusted_hosts) if trusted_hosts is not None else None
    )

    async def _hook(request: httpx.Request) -> None:
        correlation_id_request_hook(request, trusted_hosts=frozen)

    event_hooks: dict[str, list] = dict(kwargs.pop("event_hooks", {}))
    request_hooks = list(event_hooks.get("request", []))
    request_hooks.append(_hook)
    event_hooks["request"] = request_hooks
    return httpx.AsyncClient(event_hooks=event_hooks, **kwargs)


def is_internal_host(url: str, internal_suffixes: tuple[str, ...]) -> bool:
    """Helper for callers — match URL host against trusted internal suffixes."""
    host = urlparse(url).hostname or ""
    return any(host.endswith(s) for s in internal_suffixes)


__all__ = [
    "correlated_async_client",
    "correlation_id_request_hook",
    "is_internal_host",
]
