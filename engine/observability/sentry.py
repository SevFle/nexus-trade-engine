from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk

from engine.config import settings
from engine.observability.redact import REDACTED, _scrub_dict, _scrub_value

logger = logging.getLogger(__name__)

# Query-string parameter names whose values are scrubbed from the request URL
# before an event is sent to Sentry.  Covers the OAuth / password-reset /
# session parameters most likely to leak credentials in a URL.
SENSITIVE_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "token",
        "api_key",
        "code",
        "reset_token",
        "session",
        "key",
        "secret",
    }
)


def _redact_url(url: Any) -> Any:
    """Replace sensitive query-string parameter values in *url*.

    Parameters named in :data:`SENSITIVE_QUERY_PARAMS` (case-insensitive)
    have their values replaced with :data:`REDACTED`.  Everything else —
    scheme, host, path, fragment, parameter order and the exact bytes of
    non-sensitive values — is preserved.  The query string is split manually
    rather than round-tripped through ``parse_qsl`` / ``urlencode`` so that
    non-sensitive values are not re-encoded and the :data:`REDACTED` marker is
    emitted verbatim.  Non-string input or URLs without a query string are
    returned untouched.
    """
    if not isinstance(url, str) or "?" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    redacted_segments: list[str] = []
    for segment in parts.query.split("&"):
        name = segment.partition("=")[0]
        if name.lower() in SENSITIVE_QUERY_PARAMS:
            redacted_segments.append(f"{name}={REDACTED}")
        else:
            redacted_segments.append(segment)
    new_query = "&".join(redacted_segments)
    return urlunsplit(parts._replace(query=new_query))


def _scrub_request(request: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from the Sentry ``request`` payload.

    * ``url``: sensitive query-string parameters are masked via
      :func:`_redact_url`.
    * ``cookies``: scrubbed with :func:`_scrub_value` (banned cookie names
      and value patterns alike).
    * ``env``: scrubbed with :func:`_scrub_dict` when present.
    """
    out = dict(request)

    url = out.get("url")
    if isinstance(url, str):
        out["url"] = _redact_url(url)

    cookies = out.get("cookies")
    if cookies is not None:
        out["cookies"] = _scrub_value(cookies)

    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = _scrub_dict(env)

    return out


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Sentry ``before_send`` hook.

    Reuses the structlog redaction logic (``engine.observability.redact``) to
    strip secrets / PII from the event's ``contexts``, ``breadcrumbs`` and
    ``request`` (URL query string, cookies and env) before it leaves the
    process.  Mirrors the guarantee the log redaction processor already
    provides for log records.
    """
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        event["contexts"] = _scrub_dict(contexts)

    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        event["breadcrumbs"] = _scrub_dict(breadcrumbs)
    elif isinstance(breadcrumbs, list):
        event["breadcrumbs"] = _scrub_value(breadcrumbs)

    request = event.get("request")
    if isinstance(request, dict):
        event["request"] = _scrub_request(request)

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


__all__ = [
    "SENSITIVE_QUERY_PARAMS",
    "_before_send",
    "_redact_url",
    "_scrub_request",
    "close_sentry",
    "init_sentry",
    "setup_sentry",
]
