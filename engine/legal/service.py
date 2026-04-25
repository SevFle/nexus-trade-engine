from __future__ import annotations

import uuid  # noqa: TC003
from datetime import UTC
from datetime import datetime as dt
from typing import TYPE_CHECKING

import structlog
from fastapi import HTTPException
from packaging.version import Version
from sqlalchemy import select

from engine.db.models import (
    DataProviderAttribution,
    LegalAcceptance,
    LegalDocument,
)
from engine.legal.schemas import (
    AcceptanceItem,
    AcceptanceRecord,
    AcceptedItem,
    AttributionItem,
    DocumentSummary,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

NUM_DOCS_ON_DISK = 6


def _version_gte(accepted: str, current: str) -> bool:
    return Version(accepted) >= Version(current)


def _version_lt(accepted: str, current: str) -> bool:
    return Version(accepted) < Version(current)


async def _bulk_latest_acceptances(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> dict[str, LegalAcceptance]:
    latest_cte = (
        select(
            LegalAcceptance.document_slug,
            LegalAcceptance.accepted_at,
        )
        .where(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.revoked_at.is_(None),
        )
        .order_by(LegalAcceptance.document_slug, LegalAcceptance.accepted_at.desc())
        .distinct(LegalAcceptance.document_slug)
        .cte("latest_acceptance")
    )
    stmt = select(LegalAcceptance).where(
        LegalAcceptance.user_id == user_id,
        LegalAcceptance.revoked_at.is_(None),
        LegalAcceptance.document_slug.in_(select(latest_cte.c.document_slug)),
        LegalAcceptance.accepted_at.in_(
            select(latest_cte.c.accepted_at).where(
                latest_cte.c.document_slug == LegalAcceptance.document_slug
            )
        ),
    )
    result = await db.execute(stmt)
    return {la.document_slug: la for la in result.scalars().all()}


async def list_documents(
    db: AsyncSession,
    user_id: uuid.UUID | None = None,
    category: str | None = None,
) -> list[DocumentSummary]:
    stmt = select(LegalDocument).order_by(LegalDocument.display_order)
    if category:
        stmt = stmt.where(LegalDocument.category == category)
    result = await db.execute(stmt)
    docs = result.scalars().all()

    acceptances: dict[str, LegalAcceptance] = {}
    if user_id:
        acceptances = await _bulk_latest_acceptances(db, user_id)

    summaries: list[DocumentSummary] = []
    for doc in docs:
        accepted = False
        accepted_version: str | None = None
        la = acceptances.get(doc.slug)
        if user_id and la is not None:
            accepted = True
            accepted_version = la.document_version
        needs_re = doc.requires_acceptance and (
            not accepted
            or (
                accepted_version is not None and _version_lt(accepted_version, doc.current_version)
            )
        )
        summaries.append(
            DocumentSummary(
                slug=doc.slug,
                title=doc.title,
                current_version=doc.current_version,
                effective_date=doc.effective_date,
                requires_acceptance=doc.requires_acceptance,
                category=doc.category,
                accepted=accepted,
                accepted_version=accepted_version,
                needs_re_acceptance=bool(needs_re),
            )
        )
    return summaries


async def get_document_content(
    db: AsyncSession,
    slug: str,
    version: str | None = None,
) -> tuple[LegalDocument, str] | None:
    stmt = select(LegalDocument).where(LegalDocument.slug == slug)
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if doc is None:
        return None
    if version and version != doc.current_version:
        return None
    try:
        with open(doc.file_path) as f:
            markdown = f.read()
    except FileNotFoundError:
        logger.exception("legal.file_not_found", path=doc.file_path, slug=slug)
        return None
    return doc, markdown


async def record_acceptances(
    db: AsyncSession,
    user_id: uuid.UUID,
    items: list[AcceptanceItem],
    ip_address: str,
    user_agent: str,
    context: str | None = None,
) -> list[AcceptedItem]:
    accepted: list[AcceptedItem] = []
    now = dt.now(tz=UTC)

    for item in items:
        doc_stmt = select(LegalDocument).where(
            LegalDocument.slug == item.document_slug,
            LegalDocument.current_version == item.document_version,
        )
        doc_result = await db.execute(doc_stmt)
        doc = doc_result.scalar_one_or_none()
        if doc is None:
            slug_ver = f"{item.document_slug} v{item.document_version}"
            raise HTTPException(
                status_code=422,
                detail=f"Document '{slug_ver}' not found",
            )

        existing = await _get_exact_acceptance(
            db, user_id, item.document_slug, item.document_version
        )
        if existing is not None:
            accepted.append(
                AcceptedItem(
                    document_slug=item.document_slug,
                    document_version=item.document_version,
                    accepted_at=existing.accepted_at,
                )
            )
            continue

        accept_context = context
        if accept_context is None:
            has_prior = await _get_latest_acceptance(db, user_id, item.document_slug)
            accept_context = "onboarding" if has_prior is None else "re-acceptance"

        record = LegalAcceptance(
            user_id=user_id,
            document_slug=item.document_slug,
            document_version=item.document_version,
            accepted_at=now,
            ip_address=ip_address,
            user_agent=user_agent,
            context=accept_context,
        )
        db.add(record)
        accepted.append(
            AcceptedItem(
                document_slug=item.document_slug,
                document_version=item.document_version,
                accepted_at=now,
            )
        )

    await db.flush()
    return accepted


async def list_user_acceptances(
    db: AsyncSession,
    user_id: uuid.UUID,
    document_slug: str | None = None,
) -> list[AcceptanceRecord]:
    stmt = (
        select(LegalAcceptance)
        .where(LegalAcceptance.user_id == user_id)
        .order_by(LegalAcceptance.accepted_at.desc())
    )
    if document_slug:
        stmt = stmt.where(LegalAcceptance.document_slug == document_slug)
    result = await db.execute(stmt)
    records = result.scalars().all()
    return [
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


async def get_pending_acceptances(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[LegalDocument]:
    stmt = select(LegalDocument).where(LegalDocument.requires_acceptance.is_(True))
    result = await db.execute(stmt)
    docs = result.scalars().all()

    acceptances = await _bulk_latest_acceptances(db, user_id)

    pending: list[LegalDocument] = []
    for doc in docs:
        la = acceptances.get(doc.slug)
        if la is None or _version_lt(la.document_version, doc.current_version):
            pending.append(doc)
    return pending


async def list_attributions(
    db: AsyncSession,
    context: str | None = None,
) -> list[AttributionItem]:
    stmt = select(DataProviderAttribution).where(DataProviderAttribution.is_active.is_(True))
    result = await db.execute(stmt)
    attributions = result.scalars().all()

    items: list[AttributionItem] = []
    for attr in attributions:
        if context:
            contexts = attr.display_contexts if isinstance(attr.display_contexts, list) else []
            if context not in contexts:
                continue
        items.append(
            AttributionItem(
                provider_slug=attr.provider_slug,
                provider_name=attr.provider_name,
                attribution_text=attr.attribution_text,
                attribution_url=attr.attribution_url,
                logo_path=attr.logo_path,
            )
        )
    return items


async def _get_latest_acceptance(
    db: AsyncSession,
    user_id: uuid.UUID,
    document_slug: str,
) -> LegalAcceptance | None:
    stmt = (
        select(LegalAcceptance)
        .where(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.document_slug == document_slug,
            LegalAcceptance.revoked_at.is_(None),
        )
        .order_by(LegalAcceptance.accepted_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_exact_acceptance(
    db: AsyncSession,
    user_id: uuid.UUID,
    document_slug: str,
    document_version: str,
) -> LegalAcceptance | None:
    stmt = (
        select(LegalAcceptance)
        .where(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.document_slug == document_slug,
            LegalAcceptance.document_version == document_version,
            LegalAcceptance.revoked_at.is_(None),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
