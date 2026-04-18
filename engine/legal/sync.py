from __future__ import annotations

import re
from datetime import date as date_type
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from engine.config import settings
from engine.db.models import LegalDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_front_matter(content: str) -> dict[str, str] | None:
    match = FRONT_MATTER_RE.match(content)
    if not match:
        return None
    metadata: dict[str, str] = {}
    for line in match.group(1).strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata


def _parse_date(value: str) -> date_type:
    try:
        parts = value.split("-")
        return date_type(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError) as exc:
        msg = f"Invalid date format: {value!r}"
        raise ValueError(msg) from exc


async def sync_legal_documents(db: AsyncSession) -> int:
    legal_dir = Path(settings.legal_documents_dir)
    if not legal_dir.is_dir():
        logger.warning("legal.directory_not_found", path=str(legal_dir))
        return 0

    synced = 0
    for md_file in sorted(legal_dir.glob("*.md")):
        try:
            content = md_file.read_text()
            meta = parse_front_matter(content)
            if meta is None:
                logger.warning("legal.no_front_matter", file=str(md_file))
                continue

            slug = md_file.stem
            title = meta.get("title", slug.replace("-", " ").title())
            version = meta.get("version", "1.0.0")
            effective_date = meta.get("effective_date", "2026-01-01")
            requires_acceptance = meta.get("requires_acceptance", "true").lower() == "true"
            category = meta.get("category", "general")
            display_order = int(meta.get("display_order", "0"))

            stmt = select(LegalDocument).where(LegalDocument.slug == slug)
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing is None:
                doc = LegalDocument(
                    slug=slug,
                    title=title,
                    current_version=version,
                    effective_date=_parse_date(effective_date),
                    requires_acceptance=requires_acceptance,
                    category=category,
                    display_order=display_order,
                    file_path=str(md_file),
                )
                db.add(doc)
                logger.info("legal.document_registered", slug=slug, version=version)
            elif existing.current_version != version:
                old_version = existing.current_version
                existing.current_version = version
                existing.title = title
                existing.effective_date = _parse_date(effective_date)
                existing.requires_acceptance = requires_acceptance
                existing.category = category
                existing.display_order = display_order
                existing.file_path = str(md_file)
                logger.info(
                    "legal.version_changed",
                    slug=slug,
                    old=old_version,
                    new=version,
                )
            else:
                existing.title = title
                existing.effective_date = _parse_date(effective_date)
                existing.requires_acceptance = requires_acceptance
                existing.category = category
                existing.display_order = display_order
                existing.file_path = str(md_file)

            synced += 1
        except Exception:
            logger.exception("legal.sync_file_error", file=str(md_file))
            continue

    await db.flush()
    return synced
