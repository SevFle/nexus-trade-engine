from __future__ import annotations

import sentry_sdk

from engine.config import settings


def setup_sentry() -> None:
    """Initialise the Sentry SDK when a DSN is configured.

    When ``NEXUS_SENTRY_DSN`` is empty (the default in dev/test) this is a
    no-op, allowing the process to start without a Sentry backend.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )


def close_sentry() -> None:
    """Flush the Sentry event queue and close the client.

    Called during application shutdown so that buffered events are delivered
    before the process exits. Safe to call when Sentry was never initialised.
    """
    if not sentry_sdk.is_initialized():
        return

    sentry_sdk.flush(timeout=2)

    client = sentry_sdk.get_client()
    client.close()


__all__ = ["close_sentry", "setup_sentry"]
