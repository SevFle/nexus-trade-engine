"""httpx integration that propagates correlation id on every outbound call.

Use :func:`correlated_async_client` instead of constructing
``httpx.AsyncClient`` directly so that any tail-end service receives the
``X-Correlation-Id`` header that ties its logs back to the originating
request.
"""

from __future__ import annotations

from typing import Any

import httpx

from engine.observability import context as ctx
from engine.observability.middleware import CORRELATION_HEADER


def correlation_id_request_hook(request: httpx.Request) -> None:
    """Mutating event hook: attach the bound correlation id if missing."""
    if request.headers.get(CORRELATION_HEADER):
        return
    cid = ctx.get_correlation_id()
    if cid:
        request.headers[CORRELATION_HEADER] = cid


async def _async_request_hook(request: httpx.Request) -> None:
    correlation_id_request_hook(request)


def correlated_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` with the correlation hook attached."""
    event_hooks: dict[str, list] = dict(kwargs.pop("event_hooks", {}))
    request_hooks = list(event_hooks.get("request", []))
    request_hooks.append(_async_request_hook)
    event_hooks["request"] = request_hooks
    return httpx.AsyncClient(event_hooks=event_hooks, **kwargs)


__all__ = ["correlated_async_client", "correlation_id_request_hook"]
