from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from engine.config import settings
from engine.observability.redact import _scrub_dict, _scrub_value

logger = logging.getLogger(__name__)


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
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


def init_sentry() -> None:
    """Initialise the Sentry SDK when a DSN is configured.

    Reads the Sentry ``dsn``, ``traces_sample_rate`` and ``environment``
    (plus the app ``release`` version) from the application settings
    (pydantic-settings — see :class:`engine.config.Settings`) and hands
    them to :func:`sentry_sdk.init`. A ``before_send`` hook
    (:func:`_before_send`) is wired in to redact secrets / PII from every
    outbound event.

    This is the canonical entry point, invoked from the FastAPI lifespan
    startup (``engine.app``).

    When ``NEXUS_SENTRY_DSN`` is empty (the default in dev/test) this is a
    graceful no-op, allowing the process to start without a Sentry backend.
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


def setup_sentry() -> None:
    """Backward-compatible alias for :func:`init_sentry`.

    .. deprecated::
        Prefer :func:`init_sentry`. This alias is kept so existing call
        sites and tests continue to work unchanged.
    """
    init_sentry()


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


__all__ = ["_before_send", "close_sentry", "init_sentry", "setup_sentry"]
