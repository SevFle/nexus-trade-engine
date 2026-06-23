from __future__ import annotations

from typing import Any

import sentry_sdk

from engine.config import settings
from engine.observability.redact import _scrub_value

# Top-level keys of a Sentry event that may carry PII. Each is scrubbed
# through the shared recursive redaction engine (redact._scrub_value) so the
# banned-key / pattern rules stay defined in exactly one place.
_PII_BEARING_KEYS: tuple[str, ...] = (
    "request",
    "extra",
    "user",
    "tags",
    "message",
)


def _scrub_exception(exc: dict[str, Any]) -> dict[str, Any]:
    """Scrub local variables inside a single exception's stack frames.

    An exception value looks like::

        {"stacktrace": {"frames": [{"vars": {"password": "leak"}}, ...]}}

    ``vars`` holds the local variables captured for each frame. We run them
    through the same scrubber so no secret ever reaches Sentry even when the
    SDK (against our wishes) captured locals. Returns a *new* dict — the input
    is never mutated.
    """
    stacktrace = exc.get("stacktrace")
    if not isinstance(stacktrace, dict):
        return exc

    frames = stacktrace.get("frames")
    if not isinstance(frames, list):
        return exc

    scrubbed_frames = []
    for frame in frames:
        if isinstance(frame, dict) and "vars" in frame:
            scrubbed_frames.append(
                {**frame, "vars": _scrub_value(frame["vars"])}
            )
        else:
            scrubbed_frames.append(frame)

    return {**exc, "stacktrace": {**stacktrace, "frames": scrubbed_frames}}


def _scrub_event(event: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub every PII-bearing field of a Sentry event.

    Covers: ``request`` (URL, headers, cookies, query_string, data),
    ``extra``, ``user``, ``tags``, ``message`` and the local-variable maps
    nested under ``exception.values[*].stacktrace.frames[*].vars``.

    Returns a *new* event dict; the input event is never mutated.
    """
    out = dict(event)
    for key in _PII_BEARING_KEYS:
        if key in out:
            out[key] = _scrub_value(out[key])

    exception_block = out.get("exception")
    if isinstance(exception_block, dict):
        values = exception_block.get("values")
        if isinstance(values, list):
            out["exception"] = {
                **exception_block,
                "values": [
                    _scrub_exception(exc) if isinstance(exc, dict) else exc
                    for exc in values
                ],
            }

    return out


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """``before_send`` hook: scrub PII from error/issue events before export."""
    del hint
    return _scrub_event(event)


def _before_send_transaction(
    event: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any]:
    """``before_send_transaction`` hook: apply the same scrubbing to transactions."""
    del hint
    return _scrub_event(event)


def setup_sentry() -> None:
    """Initialise the Sentry SDK when a DSN is configured.

    When ``NEXUS_SENTRY_DSN`` is empty (the default in dev/test) this is a
    no-op, allowing the process to start without a Sentry backend.

    PII hardening:
      * ``before_send`` / ``before_send_transaction`` recursively scrub
        request payloads, user data, tags, messages and stack-frame locals.
      * ``include_local_variables=False`` disables local-variable capture at
        the SDK level as defence-in-depth.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        include_local_variables=False,
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
