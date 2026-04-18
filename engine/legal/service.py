from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, select

from engine.db.models import DataProviderAttribution, LegalAcceptance, LegalDocument

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def list_documents(
    session: AsyncSession,
    user_id: uuid.UUID | None = None,
    category: str | None = None,
) -> list[dict]:
    stmt = select(LegalDocument).order_by(LegalDocument.display_order)
    if category:
        stmt = stmt.where(LegalDocument.category == category)
    result = await session.execute(stmt)
    documents = result.scalars().all()

    response = []
    for doc in documents:
        item = {
            "slug": doc.slug,
            "title": doc.title,
            "current_version": doc.current_version,
            "effective_date": doc.effective_date,
            "requires_acceptance": doc.requires_acceptance,
            "category": doc.category,
            "accepted": False,
            "accepted_version": None,
            "needs_re_acceptance": False,
        }

        if user_id and doc.requires_acceptance:
            acc_stmt = (
                select(LegalAcceptance)
                .where(
                    and_(
                        LegalAcceptance.user_id == user_id,
                        LegalAcceptance.document_slug == doc.slug,
                        LegalAcceptance.revoked_at.is_(None),
                    )
                )
                .order_by(LegalAcceptance.accepted_at.desc())
                .limit(1)
            )
            acc_result = await session.execute(acc_stmt)
            latest = acc_result.scalar_one_or_none()

            if latest:
                item["accepted"] = True
                item["accepted_version"] = latest.document_version
                if latest.document_version != doc.current_version:
                    item["needs_re_acceptance"] = True
        elif user_id and not doc.requires_acceptance:
            item["accepted"] = True

        response.append(item)

    return response


async def get_document_content(
    session: AsyncSession,
    slug: str,
    version: str | None = None,
) -> dict | None:
    stmt = select(LegalDocument).where(LegalDocument.slug == slug)
    result = await session.execute(stmt)
    doc = result.scalar_one_or_none()
    if doc is None:
        return None

    _target_version = version or doc.current_version

    from pathlib import Path  # noqa: PLC0415

    base_dir = Path(__file__).resolve().parent.parent.parent
    file_path = base_dir / doc.file_path

    if not file_path.is_file():
        return None

    content = file_path.read_text(encoding="utf-8")

    from engine.legal.sync import apply_substitutions, parse_front_matter  # noqa: PLC0415

    try:
        _, body = parse_front_matter(content)
    except ValueError:
        body = content

    rendered = apply_substitutions(body, effective_date=str(doc.effective_date))

    return {
        "slug": doc.slug,
        "title": doc.title,
        "version": _target_version,
        "effective_date": doc.effective_date,
        "content_markdown": rendered,
        "requires_acceptance": doc.requires_acceptance,
    }


async def record_acceptances(
    session: AsyncSession,
    user_id: uuid.UUID,
    acceptances: list[dict],
    ip_address: str,
    user_agent: str,
    context: str | None = None,
) -> list[dict]:
    accepted = []
    for item in acceptances:
        slug = item["document_slug"]
        ver = item["document_version"]

        doc_stmt = select(LegalDocument).where(LegalDocument.slug == slug)
        doc_result = await session.execute(doc_stmt)
        doc = doc_result.scalar_one_or_none()
        if doc is None:
            continue
        if doc.current_version != ver:
            continue

        existing_stmt = select(LegalAcceptance).where(
            and_(
                LegalAcceptance.user_id == user_id,
                LegalAcceptance.document_slug == slug,
                LegalAcceptance.document_version == ver,
                LegalAcceptance.revoked_at.is_(None),
            )
        )
        existing_result = await session.execute(existing_stmt)
        existing = existing_result.scalar_one_or_none()

        if existing:
            accepted.append(
                {
                    "document_slug": slug,
                    "document_version": ver,
                    "accepted_at": existing.accepted_at,
                }
            )
            continue

        if context is None:
            any_prior_stmt = (
                select(func.count())
                .select_from(LegalAcceptance)
                .where(
                    and_(
                        LegalAcceptance.user_id == user_id,
                        LegalAcceptance.document_slug == slug,
                        LegalAcceptance.revoked_at.is_(None),
                    )
                )
            )
            any_prior_result = await session.execute(any_prior_stmt)
            has_prior = any_prior_result.scalar() > 0
            detected_context = "re-acceptance" if has_prior else "onboarding"
        else:
            detected_context = context

        record = LegalAcceptance(
            user_id=user_id,
            document_slug=slug,
            document_version=ver,
            ip_address=ip_address,
            user_agent=user_agent,
            context=detected_context,
        )
        session.add(record)
        await session.flush()

        accepted.append(
            {
                "document_slug": slug,
                "document_version": ver,
                "accepted_at": record.accepted_at,
            }
        )

    return accepted


async def get_user_acceptances(
    session: AsyncSession,
    user_id: uuid.UUID,
    document_slug: str | None = None,
) -> list[LegalAcceptance]:
    stmt = select(LegalAcceptance).where(LegalAcceptance.user_id == user_id)
    if document_slug:
        stmt = stmt.where(LegalAcceptance.document_slug == document_slug)
    stmt = stmt.order_by(LegalAcceptance.accepted_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_pending_acceptances(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[LegalDocument]:
    docs_stmt = select(LegalDocument).where(LegalDocument.requires_acceptance.is_(True))
    docs_result = await session.execute(docs_stmt)
    docs = docs_result.scalars().all()

    pending = []
    for doc in docs:
        acc_stmt = select(LegalAcceptance).where(
            and_(
                LegalAcceptance.user_id == user_id,
                LegalAcceptance.document_slug == doc.slug,
                LegalAcceptance.document_version == doc.current_version,
                LegalAcceptance.revoked_at.is_(None),
            )
        )
        acc_result = await session.execute(acc_stmt)
        has_accepted = acc_result.scalar_one_or_none() is not None
        if not has_accepted:
            pending.append(doc)

    return pending


async def list_attributions(
    session: AsyncSession,
    context: str | None = None,
) -> list[DataProviderAttribution]:
    stmt = select(DataProviderAttribution).where(DataProviderAttribution.is_active.is_(True))
    if context:
        stmt = stmt.where(DataProviderAttribution.display_contexts.contains([context]))
    result = await session.execute(stmt)
    return list(result.scalars().all())
