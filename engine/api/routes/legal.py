from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from engine.api.auth.dependency import get_current_user
from engine.config import settings
from engine.db.models import User
from engine.deps import get_db
from engine.legal import service as legal_service
from engine.legal.repository import get_latest_acceptance, record_acceptance
from engine.legal.schemas import (
    AcceptanceListResponse,
    AcceptRequest,
    AcceptResponse,
    AttributionListResponse,
    DocumentDetailResponse,
    DocumentListResponse,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = structlog.get_logger()

_MD_SPECIAL_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|~>])")


async def _optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Return current user if Bearer token is present and valid; else None."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    import uuid as _uuid

    from sqlalchemy import select

    from engine.api.auth.jwt import decode_token

    token = auth.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if not payload:
        return None
    sub = payload.get("sub")
    try:
        uid = _uuid.UUID(sub)
    except (ValueError, TypeError, AttributeError):
        return None
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    return user if user and user.is_active else None


def _escape_markdown(value: str) -> str:
    return _MD_SPECIAL_RE.sub(r"\\\1", value)


def _apply_substitutions(content: str, effective_date: str = "") -> str:
    return (
        content.replace("{{OPERATOR_NAME}}", _escape_markdown(settings.operator_name))
        .replace("{{OPERATOR_EMAIL}}", _escape_markdown(settings.operator_email))
        .replace("{{OPERATOR_URL}}", _escape_markdown(settings.operator_url))
        .replace("{{JURISDICTION}}", _escape_markdown(settings.jurisdiction))
        .replace("{{PLATFORM_FEE_PERCENT}}", _escape_markdown(str(settings.platform_fee_percent)))
        .replace("{{EFFECTIVE_DATE}}", effective_date)
    )


def _strip_front_matter(text: str) -> str:
    first = text.find("---\n")
    if first == -1:
        return text
    second = text.find("---\n", first + 4)
    if second == -1:
        return text
    return text[second + 4 :].strip()


@router.get("/api/v1/legal/documents", response_model=DocumentListResponse)
async def list_documents(
    category: str | None = Query(None),
    user: User | None = Depends(_optional_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    user_id = user.id if user else None
    summaries = await legal_service.list_documents(db, user_id=user_id, category=category)
    return DocumentListResponse(documents=summaries)


@router.get(
    "/api/v1/legal/documents/{slug}",
    response_model=DocumentDetailResponse,
)
async def get_document(
    slug: str = Path(pattern=r"^[a-z0-9-]+$"),
    version: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> DocumentDetailResponse:
    result = await legal_service.get_document_content(db, slug, version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Document '{slug}' not found")
    doc, raw = result
    rendered = _strip_front_matter(_apply_substitutions(raw, str(doc.effective_date)))
    return DocumentDetailResponse(
        slug=doc.slug,
        title=doc.title,
        version=doc.current_version,
        effective_date=doc.effective_date,
        content_markdown=rendered,
        requires_acceptance=doc.requires_acceptance,
    )


@router.post("/api/v1/legal/accept", response_model=AcceptResponse)
async def accept_documents(
    request: Request,
    body: AcceptRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AcceptResponse:
    ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    accepted = await legal_service.record_acceptances(
        db,
        user_id=user.id,
        items=body.acceptances,
        ip_address=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return AcceptResponse(accepted=accepted)


@router.get("/api/v1/legal/acceptances/me", response_model=AcceptanceListResponse)
async def list_my_acceptances(
    document_slug: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AcceptanceListResponse:
    acceptances = await legal_service.list_user_acceptances(
        db, user_id=user.id, document_slug=document_slug
    )
    return AcceptanceListResponse(acceptances=acceptances)


@router.get("/api/v1/legal/attributions", response_model=AttributionListResponse)
async def list_attributions(
    context: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> AttributionListResponse:
    items = await legal_service.list_attributions(db, context=context)
    return AttributionListResponse(attributions=items)


# --------------------------------------------------------------------------- #
# Legal Gate acceptance-tracking slice
# --------------------------------------------------------------------------- #
# Lean, append-only acceptance log keyed by user. Distinct from the document-
# management ``/api/v1/legal/*`` surface above: no document body storage,
# just the audit facts (who / which version / when / from where).
# Persistence: engine.legal.models.LegalAcceptance (table legal_gate_acceptances).
# These routes are registered automatically because this ``router`` is already
# included by the API gateway (engine.api.router.api_router).
class GateAcceptRequest(BaseModel):
    """Request body for ``POST /api/legal/accept``."""

    model_config = ConfigDict(extra="forbid")

    document_version: str = Field(
        ...,
        min_length=1,
        description="Version of the legal document being accepted.",
    )


class GateAcceptanceOut(BaseModel):
    """Serialized acceptance record returned by the accept endpoint."""

    user_id: str
    document_version: str
    accepted_at: datetime
    ip_address: str


class GateAcceptResponse(BaseModel):
    accepted: bool
    acceptance: GateAcceptanceOut


class GateStatusResponse(BaseModel):
    accepted: bool = Field(description="True iff the user accepted the current version.")
    current_version: str
    accepted_version: str | None = None
    accepted_at: datetime | None = None
    needs_acceptance: bool


@router.post("/api/legal/accept", response_model=GateAcceptResponse)
async def record_legal_gate_acceptance(
    request: Request,
    body: GateAcceptRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GateAcceptResponse:
    """Record the authenticated user's acceptance of a legal document version.

    Appends a new audit row (every acceptance is retained for a lossless
    audit trail). The transaction is committed by the ``get_db`` dependency
    after the route returns; here we only ``flush`` (via the repository) so
    the row is visible to any same-request follow-up read.
    """
    ip = request.client.host if request.client else "unknown"
    record = await record_acceptance(db, str(user.id), body.document_version, ip)
    logger.info(
        "legal.gate_acceptance_recorded",
        user_id=str(user.id),
        document_version=body.document_version,
    )
    return GateAcceptResponse(
        accepted=True,
        acceptance=GateAcceptanceOut(
            user_id=record.user_id,
            document_version=record.document_version,
            accepted_at=record.accepted_at,
            ip_address=record.ip_address,
        ),
    )


@router.get("/api/legal/status", response_model=GateStatusResponse)
async def get_legal_gate_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GateStatusResponse:
    """Report the authenticated user's current legal-gate acceptance status.

    ``accepted`` is ``True`` only when the user's most recent acceptance
    matches ``settings.legal_terms_version``; otherwise re-acceptance is
    required (``needs_acceptance=True``).
    """
    current_version = settings.legal_terms_version
    latest = await get_latest_acceptance(db, str(user.id))
    accepted_version = latest.document_version if latest is not None else None
    accepted = accepted_version == current_version
    return GateStatusResponse(
        accepted=accepted,
        current_version=current_version,
        accepted_version=accepted_version,
        accepted_at=latest.accepted_at if latest is not None else None,
        needs_acceptance=not accepted,
    )
