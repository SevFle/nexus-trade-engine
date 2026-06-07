"""Correlation ID middleware — public API + cross-service helpers.

Re-exports the canonical
:class:`~engine.observability.middleware.CorrelationIdMiddleware` so
downstream callers can import the entire Phase 2 cross-cutting stack
from a single namespace:

    from engine.api.middleware import CorrelationIdMiddleware, RedisBucketBackend

Adds :func:`propagate_headers`, a small helper for fan-out contexts
where the request handler spawns child HTTP calls or background tasks
and must carry the correlation id forward. The helper is intentionally
framework-agnostic — it does not depend on httpx so it works equally
well for ``aiohttp``, ``urllib``, or hand-built outgoing payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.observability import context as ctx
from engine.observability.middleware import (
    CORRELATION_HEADER,
    MAX_CORRELATION_ID_LENGTH,
    CorrelationIdMiddleware,
    _safe_correlation_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def propagate_headers(
    headers: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a headers dict with the active correlation id stamped on it.

    Use this when issuing an outbound HTTP call from inside a request
    handler or background task. The returned dict is a fresh copy —
    mutating it does not affect the input.

    If the caller already supplied a header it is preserved verbatim
    (subject to validation); otherwise we pull the active id from the
    contextvars scope. When neither source has an id we generate a
    fresh one and bind it so subsequent calls within the same task see
    the same value.

    Parameters
    ----------
    headers:
        Caller-supplied outbound headers. Pass ``None`` (default) when
        starting from scratch.
    extra:
        Optional additional headers merged last. Useful for stamping
        ``X-Request-Id`` alongside the correlation id without forcing
        every caller to know which fields the downstream service wants.
    """
    merged: dict[str, str] = dict(headers) if headers else {}

    incoming = merged.get(CORRELATION_HEADER)
    if incoming:
        # Validate even on the outbound path — a buggy caller could
        # have stuffed an unvalidated id into the headers map.
        validated = _safe_correlation_id(incoming)
        if validated != incoming:
            merged[CORRELATION_HEADER] = validated
    else:
        cid = ctx.get_correlation_id() or ctx.ensure_correlation_id()
        merged[CORRELATION_HEADER] = cid

    if extra:
        for k, v in extra.items():
            merged.setdefault(k, v)
    return merged


def current_correlation_id() -> str | None:
    """Thin convenience wrapper around the context module."""
    return ctx.get_correlation_id()


__all__ = [
    "CORRELATION_HEADER",
    "MAX_CORRELATION_ID_LENGTH",
    "CorrelationIdMiddleware",
    "current_correlation_id",
    "propagate_headers",
]
