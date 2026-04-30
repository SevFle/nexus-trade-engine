from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from engine.config import settings
from engine.observability.processors import (
    add_correlation_context,
    add_service_metadata,
    sampling_filter,
)
from engine.observability.redact import redact_processor


def _build_handler() -> logging.Handler:
    sink = settings.log_sink.lower()
    if sink == "file":
        path = Path(settings.log_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return logging.handlers.WatchedFileHandler(path, encoding="utf-8")
    # otlp routing happens via the OTel logs SDK; until that's wired in,
    # otlp falls back to stdout so logs are never silently dropped.
    return logging.StreamHandler(sys.stdout)


def setup_logging() -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        sampling_filter,
        add_service_metadata,
        add_correlation_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_processor,
    ]

    if settings.log_format == "json" or settings.is_production:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = _build_handler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level)
