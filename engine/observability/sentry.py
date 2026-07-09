from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import sentry_sdk

from engine.config import settings
from engine.observability.redact import (
    REDACTED,
    _is_banned,
    _scrub_dict,
    _scrub_string,
    _scrub_value,
)

logger = logging.getLogger(__name__)


def _redact_query_string(qs: str) -> str:
    r"""Redact secret-bearing parameters from a URL query string.

    Splits on ``&`` / the first ``=`` so every parameter is considered
    in isolation. The generic :func:`_scrub_string` is *not* enough on a
    raw query string: its inline ``key=value`` rule uses a greedy
    ``\S+`` for the value, which lets one parameter absorb its siblings
    across the ``&`` (e.g. ``token=secret&page=1`` would lose ``page=1``).

    A parameter whose *decoded* key is sensitive (per :func:`_is_banned`)
    has its value replaced wholesale with :data:`REDACTED`; any other
    parameter's value is still passed through :func:`_scrub_string` so
    value-level patterns (Bearer tokens, PANs, prefixed secrets) are
    caught. Non-sensitive parameters are preserved verbatim.
    """
    segments: list[str] = []
    for raw in qs.split("&"):
        key, sep, value = raw.partition("=")
        if sep:
            if _is_banned(urllib.parse.unquote_plus(key)):
                redacted_value = REDACTED
            else:
                redacted_value = _scrub_string(value)
            segments.append(f"{key}{sep}{redacted_value}")
        else:
            # Flag-style parameter (no '=') or empty segment.
            segments.append(_scrub_string(key))
    return "&".join(segments)


def _scrub_request(request: dict[str, Any]) -> dict[str, Any]:
    """Scrub secrets / PII from a Sentry ``request`` interface payload.

    Sentry's HTTP integrations attach a ``request`` object to events
    holding ``url``, ``method``, ``query_string``, ``headers`` and
    ``data`` (the body). The generic :func:`_scrub_dict` walk used for
    ``contexts`` is not enough here:

    * ``query_string`` is a *single string* whose sensitive params would
      be missed (or mangled) -- :func:`_redact_query_string` parses it
      parameter by parameter.
    * ``headers`` is a flat ``{name: value}`` dict scrubbed with
      :func:`_scrub_dict`, which covers the standard sensitive headers
      (``authorization``, ``cookie``, ``set-cookie``, ``x-api-key``,
      ``x-auth-token``, ``proxy-authorization``) via :func:`_is_banned`.
    * ``data`` (the request body) may be a dict / list / str / bytes.
      A *string* body is typically form-encoded (``key=value&...``) and
      is scrubbed parameter by parameter with :func:`_redact_query_string`
      (the same helper used for ``query_string``) so the greedy inline-key
      rule in :func:`_scrub_string` cannot absorb non-sensitive siblings
      (e.g. ``password=secret&keep=ok`` -> ``password=***REDACTED***&keep=ok``).
      A *bytes* body is decoded, scrubbed the same way and re-encoded so
      the field stays ``bytes``. Dict / list bodies are scrubbed
      recursively with :func:`_scrub_value`.

    All other request fields (``url``, ``method``, ``env`` ...) are
    passed through unchanged. Returns a *new* dict; the input is not
    mutated.
    """
    out = dict(request)

    query_string = out.get("query_string")
    if isinstance(query_string, str):
        out["query_string"] = _redact_query_string(query_string)

    headers = out.get("headers")
    if isinstance(headers, dict):
        out["headers"] = _scrub_dict(headers)

    data = out.get("data")
    if isinstance(data, str):
        out["data"] = _redact_query_string(data)
    elif isinstance(data, bytes):
        decoded = data.decode("utf-8", errors="replace")
        out["data"] = _redact_query_string(decoded).encode("utf-8")
    elif data is not None:
        out["data"] = _scrub_value(data)

    return out


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Sentry ``before_send`` hook.

    Reuses the structlog redaction logic (``engine.observability.redact``) to
    strip secrets / PII from the event's ``contexts``, ``breadcrumbs`` and
    ``request`` payload before it leaves the process.  Mirrors the guarantee
    the log redaction processor already provides for log records.
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
    "_before_send",
    "_redact_query_string",
    "_scrub_request",
    "close_sentry",
    "init_sentry",
    "setup_sentry",
]
