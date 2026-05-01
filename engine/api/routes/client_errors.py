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
"""

from __future__ import annotations

import uuid
from http import HTTPStatus

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter()
logger = structlog.get_logger()


_MAX_TEXT = 64 * 1024  # 64 KiB cap per text field — guard against log-bombing.


class ClientErrorReport(BaseModel):
    """Inbound payload from a frontend ErrorBoundary."""

    message: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    stack: str | None = Field(default=None, max_length=_MAX_TEXT)
    component_stack: str | None = Field(default=None, max_length=_MAX_TEXT)
    url: str | None = Field(default=None, max_length=2048)
    user_agent: str | None = Field(default=None, max_length=1024)
    # Caller-supplied id lets the UI print it to the user before the
    # POST returns; if omitted we generate one server-side.
    error_id: str | None = Field(default=None, max_length=128)


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
        message=payload.message,
        stack=payload.stack,
        component_stack=payload.component_stack,
        url=payload.url,
        user_agent=payload.user_agent,
        client_host=client_host,
    )
    return ClientErrorAck(error_id=error_id)


__all__ = ["ClientErrorAck", "ClientErrorReport", "router"]
