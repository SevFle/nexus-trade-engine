"""OpenTelemetry tracing bootstrap.

``setup_tracing`` installs a process-wide :class:`TracerProvider` backed
by a :class:`~opentelemetry.sdk.resources.Resource` describing the
service (name / version / environment) and — when an OTLP collector is
reachable — a ``BatchSpanProcessor`` that exports spans over OTLP/gRPC.

The function returns :class:`TracingHooks`: thin, idempotent callables
that the FastAPI lifespan uses to instrument the app and the SQLAlchemy
engine *after* the provider has been registered, avoiding import-time
side effects when this module is merely imported (e.g. by tests).

Scope note: this module is intentionally OTel-only. Prometheus metrics
live in ``engine.observability.prometheus`` and Sentry error reporting
in ``engine.observability.sentry``; they are wired separately.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.resources import (
    DEPLOYMENT_ENVIRONMENT,
    SERVICE_NAME,
    SERVICE_VERSION,
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider

from engine.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TracingHooks:
    """Instrumentation callables returned by :func:`setup_tracing`.

    ``instrument_fastapi`` wraps a FastAPI/Starlette ``app`` so every
    inbound HTTP request becomes a span; ``instrument_sqlalchemy`` wraps
    a (sync or async) SQLAlchemy engine so every DB query becomes a
    child span.  Both wrap the OTel instrumentors which are themselves
    idempotent, so calling them more than once is a safe no-op.
    """

    instrument_fastapi: Callable[..., Any]
    instrument_sqlalchemy: Callable[..., Any]


def _build_resource() -> Resource:
    """Build the OTel :class:`Resource` from service settings.

    The resource tags every span with the service identity so a collector
    can attribute traces to this process unambiguously. Values come from
    ``settings`` (env-driven) rather than being hardcoded.
    """
    return Resource.create(
        {
            SERVICE_NAME: settings.app_name,
            SERVICE_VERSION: settings.app_version,
            DEPLOYMENT_ENVIRONMENT: settings.app_env,
        }
    )


def _instrument_fastapi(app: Any) -> None:
    """Wrap a FastAPI/Starlette app with request-spanning middleware."""
    from opentelemetry.instrumentation.fastapi import (  # noqa: PLC0415
        FastAPIInstrumentor,
    )

    FastAPIInstrumentor.instrument_app(app)


def _instrument_sqlalchemy(engine: Any) -> None:
    """Wrap a SQLAlchemy engine so each query emits a span.

    Accepts either a synchronous engine or an ``AsyncEngine`` (the async
    variant exposes its underlying sync engine via ``.sync_engine``).
    """
    from opentelemetry.instrumentation.sqlalchemy import (  # noqa: PLC0415
        SQLAlchemyInstrumentor,
    )

    sync_engine = getattr(engine, "sync_engine", engine)
    SQLAlchemyInstrumentor().instrument(engine=sync_engine)


def setup_tracing() -> TracingHooks:
    """Initialise the global tracer provider and return instrumentation hooks.

    Behaviour:

    * A :class:`TracerProvider` is built with a :class:`Resource`
      carrying ``service.name`` / ``service.version`` /
      ``deployment.environment``.
    * When ``settings.otlp_endpoint`` is non-empty, spans are exported
      via an OTLP/gRPC :class:`BatchSpanProcessor`. When the endpoint is
      empty the provider still runs but has no exporter — tracing is a
      *graceful no-op*, so the process starts with no collector present.
    * **Idempotent**: if a real ``TracerProvider`` already backs the
      OTel API (a prior call, or an auto-instrumentor) this function
      does not replace it — the OTel API would only log a warning and
      ignore the call — and simply returns the instrumentation hooks.

    Returns a :class:`TracingHooks` carrying ``instrument_fastapi`` and
    ``instrument_sqlalchemy`` callables for the app lifespan to invoke.
    """
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Tracing is already configured in this process; do not replace
        # the provider (the OTel API ignores a second set call anyway)
        # and just hand back the hooks so the caller can instrument.
        return TracingHooks(
            instrument_fastapi=_instrument_fastapi,
            instrument_sqlalchemy=_instrument_sqlalchemy,
        )

    provider = TracerProvider(resource=_build_resource())

    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("tracing.otlp_configured", endpoint=settings.otlp_endpoint)
    else:
        logger.debug("tracing.otlp_disabled_no_endpoint")

    trace.set_tracer_provider(provider)

    return TracingHooks(
        instrument_fastapi=_instrument_fastapi,
        instrument_sqlalchemy=_instrument_sqlalchemy,
    )


__all__ = ["TracingHooks", "setup_tracing"]
