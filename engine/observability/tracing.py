from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from engine.config import settings

_TRACER_NAME = "nexus.engine"


def get_tracer(name: str = _TRACER_NAME) -> trace.Tracer:
    return trace.get_tracer(name)


def setup_tracing() -> None:
    provider = TracerProvider()

    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
