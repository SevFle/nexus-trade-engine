from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from engine.api.auth.dependency import get_current_user
from engine.config import settings
from engine.db.models import User
from engine.deps import get_db
from engine.legal import service as legal_service
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
