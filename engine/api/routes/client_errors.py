"""Client-side error reporting endpoint (gh#153 server-side slice).

The frontend's top-level / per-route ``ErrorBoundary`` reports unhandled
exceptions here so we can correlate browser-side failures with the
audit trail. Persistence is intentionally not part of this slice — the
endpoint emits a structured log event via the existing observability
stack and returns the stable ``error_id`` to the caller. A follow-up
PR can sink the structlog stream into a queryable store.

The endpoint is *not* auth-gated: an authenticated session is exactly
when error reporting is most likely to fail. Abuse is bounded by the
tight per-route rate limit configured in ``engine/app.py``.

Sanitization (defence-in-depth even though structlog's default JSON
renderer would already escape these):

- ASCII control characters (CR, LF, NUL, ESC, DEL, etc.) **and** the
  Unicode C1 control-character range (U+0080-U+009F) are stripped from
  every inbound string before logging. The C1 range covers terminal-
  control sequences that some terminals interpret even when the ESC
  byte is not present (e.g. ``\u009b`` is a legacy CSI lead byte on
  8-bit-clean terminals). Both are collapsed to a single space so
  human-readable text remains legible.
- Bidirectional-formatting overrides (``U+202A-U+202E`` — including
  the well-known RTL override ``U+202E``) and the directional isolates
  ``U+2066-U+2069`` are also stripped. A leading ``\u202E`` would
  otherwise cause many terminal emulators and web-based log viewers
  to render the rest of the line right-to-left, hiding a payload
  behind legitimate-looking text (Trojan Source / CVE-2021-42574
  style). Removing them eliminates that class of visual deception.
- Zero-width characters (``\u200B`` ZWSP, ``\u200C`` ZWNJ, ``\u200D``
  ZWJ) and the byte-order mark / ZWNBSP (``\uFEFF``) are dropped too:
  they are invisible to humans, can carry steganographic payloads,
  and can confuse downstream substring search / dedup logic.
- ANSI CSI / OSC escape sequences (which depend on the ESC byte
  remaining intact to match) are dropped first; the broader control-
  character sweep runs second.
- Caller-supplied ``error_id`` must parse as a UUID; arbitrary opaque
  strings are rejected so an attacker cannot collide with a real
  server-generated correlation id.
- ``url`` is reduced to scheme+host+path before logging — query
  strings frequently carry auth tokens (``?token=``, ``?code=``,
  OAuth ``state``) and the boundary doesn't know to redact them.
"""

from __future__ import annotations

import re
import uuid
from http import HTTPStatus
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
logger = structlog.get_logger()


_MAX_TEXT = 64 * 1024  # 64 KiB cap per text field — guard against log-bombing.

# Strips ASCII control chars (CR, LF, NUL, DEL, etc.), the Unicode C1
# control range (U+0080-U+009F), Unicode directional overrides /
# isolates (U+202A-U+202E, U+2066-U+2069), zero-width chars
# (U+200B-U+200D), the BOM / ZWNBSP (U+FEFF), and CSI / OSC ANSI
# escape sequences. Pre-compiled at import time.
#
# The C1 range matters because some 8-bit-clean terminals interpret
# U+0080-U+009F as terminal-control bytes (e.g. U+009B acts as CSI)
# — dropping them is defence-in-depth against terminal-escape
# injection when humans tail raw logs. The directional / zero-width
# extensions close Trojan-Source-style (CVE-2021-42574) visual-
# deception and steganographic-payload vectors.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
# Keep \t (useful in stack traces). Drop:
#   - the rest of C0 (U+0000-U+001F minus \t) and DEL (U+007F)
#   - the C1 range (U+0080-U+009F) — 8-bit-clean terminals may
#     interpret these as terminal-control bytes (U+009B = CSI)
#   - directional overrides (U+202A-U+202E, including the RTL
#     override U+202E) and directional isolates (U+2066-U+2069)
#     — these silently change render direction in terminals and
#     web-based log viewers (Trojan Source / CVE-2021-42574)
#   - zero-width chars (U+200B-U+200D) and BOM / ZWNBSP (U+FEFF)
#     — invisible, useful for steganography and dedup-evading
#     collisions in downstream consumers
_CTRL_RE = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u200b-\u200d\u202a-\u202e\u2066-\u2069\ufeff]"
)


def _scrub(value: str | None) -> str | None:
    if value is None:
        return None
    # Drop ANSI sequences first (need the ESC byte intact to match),
    # then collapse remaining ASCII + Unicode C1 control characters,
    # directional overrides, and zero-width / BOM characters into
    # spaces.
    return _CTRL_RE.sub(" ", _ANSI_RE.sub("", value))


def _strip_query(url: str | None) -> str | None:
    """Return ``url`` with query + fragment removed. Logs see only
    scheme + host + path so accidentally-captured auth tokens in
    query strings do not flow into the audit trail."""
    if url is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class ClientErrorReport(BaseModel):
    """Inbound payload from a frontend ErrorBoundary."""

    message: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    stack: str | None = Field(default=None, max_length=_MAX_TEXT)
    component_stack: str | None = Field(default=None, max_length=_MAX_TEXT)
    url: str | None = Field(default=None, max_length=2048)
    user_agent: str | None = Field(default=None, max_length=1024)
    # Caller-supplied id lets the UI print it to the user before the
    # POST returns; if omitted we generate one server-side. We require
    # UUID shape so an attacker cannot fabricate an id that collides
    # with a real server-generated correlation id.
    error_id: str | None = Field(default=None, max_length=128)

    @field_validator("error_id")
    @classmethod
    def _validate_error_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(
                "error_id must be a UUID"
            ) from exc
        return v


class ClientErrorAck(BaseModel):
    error_id: str


@router.post(
    "/errors",
    status_code=HTTPStatus.CREATED,
    response_model=ClientErrorAck,
)
async def report_client_error(
    payload: ClientErrorReport, request: Request
) -> ClientErrorAck:
    error_id = payload.error_id or str(uuid.uuid4())
    client_host = request.client.host if request.client else None
    logger.error(
        "client.error",
        error_id=error_id,
        message=_scrub(payload.message),
        stack=_scrub(payload.stack),
        component_stack=_scrub(payload.component_stack),
        url=_scrub(_strip_query(payload.url)),
        user_agent=_scrub(payload.user_agent),
        client_host=client_host,
    )
    return ClientErrorAck(error_id=error_id)


__all__ = ["ClientErrorAck", "ClientErrorReport", "router"]
