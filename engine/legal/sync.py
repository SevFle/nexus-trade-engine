from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from engine.config import settings
from engine.db.models import LegalDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

LEGAL_DIR = Path(__file__).resolve().parent.parent.parent / "legal"


def _get_substitutions() -> dict[str, str]:
    return {
        "{{OPERATOR_NAME}}": settings.operator_name,
        "{{OPERATOR_EMAIL}}": settings.operator_email,
        "{{OPERATOR_URL}}": settings.operator_url,
        "{{JURISDICTION}}": settings.operator_jurisdiction,
    }


def parse_front_matter(content: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not match:
        raise ValueError("Missing or malformed YAML front-matter")

    import yaml  # noqa: PLC0415

    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict):
        raise TypeError("Front-matter must be a YAML mapping")

    body = match.group(2)
    return meta, body


def apply_substitutions(text: str, effective_date: str | None = None) -> str:
    subs = _get_substitutions()
    if effective_date:
        subs["{{EFFECTIVE_DATE}}"] = effective_date
    for marker, value in subs.items():
        text = text.replace(marker, value)
    return text


def slug_from_filename(filename: str) -> str:
    return Path(filename).stem


async def sync_legal_documents(session: AsyncSession) -> list[LegalDocument]:
    if not LEGAL_DIR.is_dir():
        logger.warning("legal_dir_not_found", path=str(LEGAL_DIR))
        return []

    docs: list[LegalDocument] = []

    for md_file in sorted(LEGAL_DIR.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            meta, _ = parse_front_matter(content)

            slug = slug_from_filename(md_file.name)
            title = meta.get("title", slug.replace("-", " ").title())
            version = str(meta.get("version", "1.0.0"))
            eff_date_raw = meta.get("effective_date", "2026-04-20")
            effective_date = (
                eff_date_raw
                if isinstance(eff_date_raw, date)
                else date.fromisoformat(str(eff_date_raw))
            )
            requires_acceptance = bool(meta.get("requires_acceptance", True))
            category = str(meta.get("category", "general"))
            display_order = int(meta.get("display_order", 0))
            file_path = f"legal/{md_file.name}"

            stmt = select(LegalDocument).where(LegalDocument.slug == slug)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing is None:
                doc = LegalDocument(
                    slug=slug,
                    title=title,
                    current_version=version,
                    effective_date=effective_date,
                    requires_acceptance=requires_acceptance,
                    category=category,
                    display_order=display_order,
                    file_path=file_path,
                )
                session.add(doc)
                docs.append(doc)
                logger.info(
                    "legal.document_registered",
                    slug=slug,
                    version=version,
                )
            elif existing.current_version != version:
                old_version = existing.current_version
                existing.current_version = version
                existing.effective_date = effective_date
                existing.title = title
                existing.requires_acceptance = requires_acceptance
                existing.category = category
                existing.display_order = display_order
                existing.file_path = file_path
                docs.append(existing)
                logger.info(
                    "legal.version_changed",
                    slug=slug,
                    old_version=old_version,
                    new_version=version,
                )
            else:
                docs.append(existing)

        except Exception:
            logger.exception("legal.sync_file_failed", file=md_file.name)

    await session.flush()
    return docs
