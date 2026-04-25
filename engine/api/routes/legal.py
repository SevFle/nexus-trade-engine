from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from engine.config import settings
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


def _apply_substitutions(content: str, effective_date: str = "") -> str:
    return (
        content.replace("{{OPERATOR_NAME}}", settings.operator_name)
        .replace("{{OPERATOR_EMAIL}}", settings.operator_email)
        .replace("{{OPERATOR_URL}}", settings.operator_url)
        .replace("{{JURISDICTION}}", settings.jurisdiction)
        .replace("{{PLATFORM_FEE_PERCENT}}", str(settings.platform_fee_percent))
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
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> DocumentListResponse:
    summaries = await legal_service.list_documents(db, user_id=None, category=category)
    return DocumentListResponse(documents=summaries)


@router.get("/api/v1/legal/documents/{slug}", response_model=DocumentDetailResponse)
async def get_document(
    slug: str,
    version: str | None = Query(None),
    db: AsyncSession = Depends(get_db),  # noqa: B008
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
    _request: Request,
    _body: AcceptRequest,
    _db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    raise HTTPException(
        status_code=401,
        detail="Authentication required.",
    )


@router.get("/api/v1/legal/acceptances/me", response_model=AcceptanceListResponse)
async def list_my_acceptances(
    _document_slug: str | None = Query(None),
    _db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    raise HTTPException(
        status_code=401,
        detail="Authentication required.",
    )


@router.get("/api/v1/legal/attributions", response_model=AttributionListResponse)
async def list_attributions(
    context: str | None = Query(None),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AttributionListResponse:
    items = await legal_service.list_attributions(db, context=context)
    return AttributionListResponse(attributions=items)
