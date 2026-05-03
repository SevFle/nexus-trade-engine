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

- CRLF and ANSI escape sequences are stripped from every inbound
  string before logging — guards against log-line forging in plain-
  text renderers and terminal-escape injection when humans tail logs.
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

# Strips ASCII control chars (CR, LF, NUL, etc.) and CSI / OSC ANSI
# escape sequences. Pre-compiled at import time.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CTRL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # keep \t, drop LF/CR/etc.


def _scrub(value: str | None) -> str | None:
    if value is None:
        return None
    # Drop ANSI sequences first (need the ESC byte intact to match),
    # then collapse remaining control characters into spaces.
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
            raise ValueError("error_id must be a UUID") from exc
        return v


class ClientErrorAck(BaseModel):
    error_id: str


@router.post(
    "/errors",
    status_code=HTTPStatus.CREATED,
    response_model=ClientErrorAck,
)
async def report_client_error(payload: ClientErrorReport, request: Request) -> ClientErrorAck:
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
