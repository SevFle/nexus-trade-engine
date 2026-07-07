"""Structured logging + per-request correlation for the FastAPI app.

Public surface for observability. Importing this package configures nothing
by itself — call :func:`configure_logging` (alias: :func:`setup_logging`) at
app startup and add :class:`CorrelationIdMiddleware` to the ASGI stack.

Typical wiring (see ``engine/app.py``)::

    from engine.observability import (
        configure_logging,
        CorrelationIdMiddleware,
    )

    configure_logging()                  # call inside the lifespan startup
    app.add_middleware(CorrelationIdMiddleware)

The middleware reads (or generates) an ``X-Correlation-Id`` per request and
binds it — plus a per-request ``request_id`` and ``span_id`` — to a
:mod:`contextvars`-backed context. Every structlog record emitted while the
context is bound is automatically enriched with those ids via the
``add_correlation_context`` processor.
"""

from __future__ import annotations

# Import order matters here only to keep the dependency graph acyclic:
# `context` has no intra-package deps, so load it first.
from engine.observability.context import (
    bind_correlation_id,
    bind_domain_context,
    bind_request_id,
    bind_request_scope,
    bind_user_context,
    clear_context,
    ensure_correlation_id,
    get_correlation_id,
    get_request_id,
    get_span_id,
    new_span_id,
    reset_tokens,
    snapshot,
    use_correlation_id,
)
from engine.observability.logging import configure_logging, get_logger, setup_logging
from engine.observability.middleware import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    safe_correlation_id,
)

__all__ = [
    "CORRELATION_HEADER",
    "CorrelationIdMiddleware",
    "bind_correlation_id",
    "bind_domain_context",
    "bind_request_id",
    "bind_request_scope",
    "bind_user_context",
    "clear_context",
    "configure_logging",
    "ensure_correlation_id",
    "get_correlation_id",
    "get_logger",
    "get_request_id",
    "get_span_id",
    "new_span_id",
    "reset_tokens",
    "safe_correlation_id",
    "setup_logging",
    "snapshot",
    "use_correlation_id",
]
