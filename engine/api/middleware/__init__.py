"""Phase 2 cross-cutting middleware: rate limiting + correlation IDs.

This package layers on top of the existing primitives in
``engine/api/rate_limit.py`` and ``engine/observability/middleware.py``
to provide:

- :mod:`engine.api.middleware.rate_limit` — a Valkey/Redis-backed token
  bucket backend (atomic via Lua), an auth-aware keying strategy so
  authenticated callers are throttled per-user instead of per-IP, and a
  :class:`~engine.api.middleware.rate_limit.RateLimitMiddleware` that
  wires both into FastAPI without disturbing the legacy in-memory path.
- :mod:`engine.api.middleware.correlation` — re-exports the canonical
  :class:`~engine.observability.middleware.CorrelationIdMiddleware` and
  adds helpers for cross-service propagation (outbound HTTP clients,
  taskiq tasks, background jobs).

The legacy single-process limiter at ``engine/api/rate_limit.py`` and
the correlation middleware at ``engine/observability/middleware.py``
remain the primary public entry points; this package only adds the
multi-pod and auth-aware extensions.
"""

from __future__ import annotations

from engine.api.middleware.correlation import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    propagate_headers,
)
from engine.api.middleware.rate_limit import (
    AuthAwareKeyFunc,
    RedisBucketBackend,
    ValkeyRateLimitMiddleware,
)
from engine.api.middleware.rate_limit import (
    RateLimitConfig as MiddlewareRateLimitConfig,
)

__all__ = [
    "CORRELATION_HEADER",
    "AuthAwareKeyFunc",
    "CorrelationIdMiddleware",
    "MiddlewareRateLimitConfig",
    "RedisBucketBackend",
    "ValkeyRateLimitMiddleware",
    "propagate_headers",
]
