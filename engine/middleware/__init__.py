"""HTTP / ASGI middleware packages for the Nexus engine API.

The default ``CorrelationIdMiddleware`` exported from this package is the
**raw-ASGI** variant (:class:`engine.observability.middleware.CorrelationIdMiddleware`),
which is what :func:`engine.app.create_app` registers. The older
``BaseHTTPMiddleware``-based implementation is available as
:class:`engine.middleware.correlation.BaseHTTPCorrelationIdMiddleware`.

The previous ``CorrelationIdMiddleware`` name that lived in
:mod:`engine.middleware.correlation` is kept as a deprecated alias for one
release cycle (it now refers to the ``BaseHTTPMiddleware`` variant and emits
a :class:`DeprecationWarning` when accessed).
"""

from __future__ import annotations

from engine.middleware.correlation import (
    CORRELATION_HEADER,
    BaseHTTPCorrelationIdMiddleware,
)
from engine.middleware.prometheus import PrometheusMiddleware
from engine.observability.middleware import CorrelationIdMiddleware

__all__ = [
    "CORRELATION_HEADER",
    "BaseHTTPCorrelationIdMiddleware",
    "CorrelationIdMiddleware",
    "PrometheusMiddleware",
]
