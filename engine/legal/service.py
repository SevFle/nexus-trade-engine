from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from engine.db.models import DataProviderAttribution, LegalAcceptance, LegalDocument

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


def _parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in version.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _version_lt(a: str, b: str) -> bool:
    return _parse_version(a) < _parse_version(b)


def _version_eq(a: str, b: str) -> bool:
    return _parse_version(a) == _parse_version(b)


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

    latest_acceptances: dict[str, LegalAcceptance] = {}
    if user_id:
        sub = (
            select(
                LegalAcceptance.document_slug,
                LegalAcceptance.document_version,
                LegalAcceptance.accepted_at,
            )
            .where(
                and_(
                    LegalAcceptance.user_id == user_id,
                    LegalAcceptance.revoked_at.is_(None),
                )
            )
            .distinct(LegalAcceptance.document_slug)
            .order_by(
                LegalAcceptance.document_slug,
                LegalAcceptance.accepted_at.desc(),
            )
        )
        sub_alias = aliased(LegalAcceptance, sub.subquery())
        la_result = await session.execute(select(sub_alias))
        for row in la_result.scalars().all():
            latest_acceptances[row.document_slug] = row

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
            latest = latest_acceptances.get(doc.slug)
            if latest:
                item["accepted"] = True
                item["accepted_version"] = latest.document_version
                if _version_lt(latest.document_version, doc.current_version):
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
    resolved_path = (base_dir / doc.file_path).resolve()
    legal_root = (base_dir / "legal").resolve()

    if not resolved_path.is_relative_to(legal_root):
        raise ValueError(f"file_path escapes legal directory: {doc.file_path}")

    if not resolved_path.is_file():
        return None

    content = resolved_path.read_text(encoding="utf-8")

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
    slugs = [a["document_slug"] for a in acceptances]
    doc_stmt = select(LegalDocument).where(LegalDocument.slug.in_(slugs))
    doc_result = await session.execute(doc_stmt)
    doc_map = {d.slug: d for d in doc_result.scalars().all()}

    existing_stmt = select(LegalAcceptance).where(
        and_(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.document_slug.in_(slugs),
            LegalAcceptance.revoked_at.is_(None),
        )
    )
    existing_result = await session.execute(existing_stmt)
    existing_map: dict[str, LegalAcceptance] = {}
    for acc in existing_result.scalars().all():
        key = f"{acc.document_slug}:{acc.document_version}"
        existing_map[key] = acc

    prior_count_stmt = (
        select(
            LegalAcceptance.document_slug,
            func.count(),
        )
        .where(
            and_(
                LegalAcceptance.user_id == user_id,
                LegalAcceptance.document_slug.in_(slugs),
                LegalAcceptance.revoked_at.is_(None),
            )
        )
        .group_by(LegalAcceptance.document_slug)
    )
    prior_result = await session.execute(prior_count_stmt)
    prior_counts: dict[str, int] = dict(prior_result.all())

    accepted = []
    for item in acceptances:
        slug = item["document_slug"]
        ver = item["document_version"]

        doc = doc_map.get(slug)
        if doc is None:
            continue
        if not _version_eq(doc.current_version, ver):
            continue

        key = f"{slug}:{ver}"
        existing = existing_map.get(key)
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
            detected_context = "re-acceptance" if prior_counts.get(slug, 0) > 0 else "onboarding"
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
    accepted_stmt = (
        select(
            LegalAcceptance.document_slug,
            LegalAcceptance.document_version,
        )
        .where(
            and_(
                LegalAcceptance.user_id == user_id,
                LegalAcceptance.revoked_at.is_(None),
            )
        )
        .distinct(LegalAcceptance.document_slug)
        .order_by(
            LegalAcceptance.document_slug,
            LegalAcceptance.accepted_at.desc(),
        )
    )
    accepted_result = await session.execute(accepted_stmt)
    accepted_versions: dict[str, str] = {
        row.document_slug: row.document_version for row in accepted_result.all()
    }

    docs_stmt = select(LegalDocument).where(LegalDocument.requires_acceptance.is_(True))
    docs_result = await session.execute(docs_stmt)
    docs = docs_result.scalars().all()

    pending = []
    for doc in docs:
        accepted_ver = accepted_versions.get(doc.slug)
        if accepted_ver is None or _version_lt(accepted_ver, doc.current_version):
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
