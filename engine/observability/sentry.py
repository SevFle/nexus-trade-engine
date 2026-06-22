from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from engine.config import settings
from engine.observability.redact import _scrub_dict, _scrub_value

logger = logging.getLogger(__name__)


def _before_send(
    event: dict[str, Any], _hint: dict[str, Any]
) -> dict[str, Any]:
    """Sentry ``before_send`` hook.

    Reuses the structlog redaction logic (``engine.observability.redact``) to
    strip secrets / PII from the event's ``contexts`` and ``breadcrumbs``
    before it leaves the process.  Mirrors the guarantee the log redaction
    processor already provides for log records.
    """
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        event["contexts"] = _scrub_dict(contexts)

    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        event["breadcrumbs"] = _scrub_dict(breadcrumbs)
    elif isinstance(breadcrumbs, list):
        event["breadcrumbs"] = _scrub_value(breadcrumbs)

    return event


def setup_sentry() -> None:
    """Initialise the Sentry SDK when a DSN is configured.

    When ``NEXUS_SENTRY_DSN`` is empty (the default in dev/test) this is a
    no-op, allowing the process to start without a Sentry backend.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        release=settings.app_version,
        environment=settings.app_env,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        before_send=_before_send,
    )


def close_sentry() -> None:
    """Flush the Sentry event queue and close the client.

    Called during application shutdown so that buffered events are delivered
    before the process exits. Safe to call when Sentry was never initialised.
    """
    if not sentry_sdk.is_initialized():
        return

    flushed = sentry_sdk.flush(timeout=2)
    if not flushed:
        logger.warning(
            "sentry.flush_timeout",
            extra={
                "detail": "Sentry failed to flush events within the "
                "2 s timeout; some events may be lost"
            },
        )

    client = sentry_sdk.get_client()
    client.close()


__all__ = ["_before_send", "close_sentry", "setup_sentry"]
