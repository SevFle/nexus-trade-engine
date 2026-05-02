"""Privacy / DSR routes — GDPR & CCPA (gh#157).

Mounted at /api/v1/privacy. Endpoints:

- POST /export             — synchronous export of the caller's data
- POST /delete             — initiate account deletion (30-day grace)
- POST /delete/cancel      — cancel deletion during the grace window
- GET  /delete/status      — pending? + remaining grace
- GET  /requests           — list the caller's DSR history
- GET  /kinds              — allow-list of DSR kinds (for clients)

Out of scope for this PR (explicit follow-ups):
- Async tarball + signed download URLs.
- Consent management (per-purpose).
- Admin-side DSR tooling for requests received outside the product.
- Restriction / objection flags.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from engine.api.auth.dependency import get_current_user
from engine.db.models import User
from engine.deps import get_db
from engine.privacy import (
    DSR_KINDS,
    cancel_deletion,
    collect_user_data,
    is_pending_deletion,
    list_user_requests,
    record_request,
    request_deletion,
)
from engine.privacy.deletion import DeletionError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(prefix="/privacy", tags=["privacy"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DSRRequestSummary(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    note: str | None
    sla_due_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime


class DSRListResponse(BaseModel):
    requests: list[DSRRequestSummary]


class ExportResponse(BaseModel):
    request: DSRRequestSummary
    data: dict[str, Any]


class DeletionRequestBody(BaseModel):
    note: str | None = Field(default=None, max_length=4000)


class DeletionStatusResponse(BaseModel):
    pending: bool
    sla_due_at: datetime | None
    request: DSRRequestSummary | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise(req) -> DSRRequestSummary:
    return DSRRequestSummary(
        id=req.id,
        kind=req.kind,
        status=req.status,
        note=req.note,
        sla_due_at=req.sla_due_at,
        completed_at=req.completed_at,
        cancelled_at=req.cancelled_at,
        created_at=req.created_at,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/export", response_model=ExportResponse)
async def export_my_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ExportResponse:
    request = await record_request(db, user_id=user.id, kind="export")
    data = await collect_user_data(db, user.id)
    request.status = "completed"
    request.completed_at = datetime.now(tz=UTC)
    await db.commit()
    return ExportResponse(request=_serialise(request), data=data)


@router.post(
    "/delete",
    response_model=DeletionStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_account_deletion(
    body: DeletionRequestBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeletionStatusResponse:
    try:
        req = await request_deletion(db, user_id=user.id, note=body.note)
    except DeletionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return DeletionStatusResponse(
        pending=True, sla_due_at=req.sla_due_at, request=_serialise(req)
    )


@router.post("/delete/cancel", response_model=DeletionStatusResponse)
async def cancel_account_deletion(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeletionStatusResponse:
    try:
        req = await cancel_deletion(db, user_id=user.id)
    except DeletionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await db.commit()
    return DeletionStatusResponse(
        pending=False, sla_due_at=None, request=_serialise(req)
    )


@router.get("/delete/status", response_model=DeletionStatusResponse)
async def deletion_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeletionStatusResponse:
    pending, sla_due_at = await is_pending_deletion(db, user.id)
    return DeletionStatusResponse(pending=pending, sla_due_at=sla_due_at, request=None)


@router.get("/requests", response_model=DSRListResponse)
async def my_dsr_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DSRListResponse:
    rows = await list_user_requests(db, user.id)
    return DSRListResponse(requests=[_serialise(r) for r in rows])


@router.get("/kinds")
async def supported_kinds() -> dict[str, list[str]]:
    """OpenAPI clients can validate ``kind`` against this allow-list."""
    return {"kinds": sorted(DSR_KINDS)}
