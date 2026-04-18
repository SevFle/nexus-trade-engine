from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam

from engine.deps import get_db
from engine.legal.schemas import (
    AcceptanceListResponse,
    AcceptanceRecord,
    AcceptedItem,
    AcceptRequest,
    AcceptResponse,
    AttributionItem,
    AttributionListResponse,
    LegalDocumentDetail,
    LegalDocumentListResponse,
    LegalDocumentSummary,
)
from engine.legal.service import (
    get_document_content,
    get_user_acceptances,
    list_attributions,
    list_documents,
    record_acceptances,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = structlog.get_logger()


@router.get("/documents", response_model=LegalDocumentListResponse)
async def get_documents(
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LegalDocumentListResponse:
    user_id: uuid.UUID | None = None

    summaries = await list_documents(db, user_id=user_id, category=category)
    return LegalDocumentListResponse(documents=[LegalDocumentSummary(**s) for s in summaries])


@router.get("/documents/{slug}", response_model=LegalDocumentDetail)
async def get_document_by_slug(
    slug: str = PathParam(pattern=r"^[a-z0-9-]+$"),
    version: str | None = Query(None),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LegalDocumentDetail:
    result = await get_document_content(db, slug, version=version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Document '{slug}' not found")
    return LegalDocumentDetail(**result)


@router.post("/accept", response_model=AcceptResponse)
async def accept_documents(
    request: Request,
    body: AcceptRequest,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AcceptResponse:
    user_id: uuid.UUID | None = None

    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    ip_address = request.headers.get(
        "x-forwarded-for", request.client.host if request.client else "unknown"
    )
    user_agent = request.headers.get("user-agent", "unknown")

    accepted = await record_acceptances(
        db,
        user_id=user_id,
        acceptances=[a.model_dump() for a in body.acceptances],
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return AcceptResponse(accepted=[AcceptedItem(**a) for a in accepted])


@router.get("/acceptances/me", response_model=AcceptanceListResponse)
async def get_my_acceptances(
    document_slug: str | None = Query(None),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AcceptanceListResponse:
    user_id: uuid.UUID | None = None

    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    records = await get_user_acceptances(db, user_id, document_slug=document_slug)
    return AcceptanceListResponse(
        acceptances=[
            AcceptanceRecord(
                id=str(r.id),
                document_slug=r.document_slug,
                document_version=r.document_version,
                accepted_at=r.accepted_at,
                context=r.context,
                revoked_at=r.revoked_at,
            )
            for r in records
        ]
    )


@router.get("/attributions", response_model=AttributionListResponse)
async def get_attributions(
    context: str | None = Query(None),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AttributionListResponse:
    attributions = await list_attributions(db, context=context)
    return AttributionListResponse(
        attributions=[
            AttributionItem(
                provider_slug=a.provider_slug,
                provider_name=a.provider_name,
                attribution_text=a.attribution_text,
                attribution_url=a.attribution_url,
                logo_path=a.logo_path,
            )
            for a in attributions
        ]
    )
