from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from engine.config import settings
from engine.db.models import LegalDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

LEGAL_DIR = Path(__file__).resolve().parent.parent.parent / "legal"

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{\}\[\]()#+\-.!|])")


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


def _get_substitutions() -> dict[str, str]:
    return {
        "{{OPERATOR_NAME}}": _escape_markdown(settings.operator_name),
        "{{OPERATOR_EMAIL}}": _escape_markdown(settings.operator_email),
        "{{OPERATOR_URL}}": _escape_markdown(settings.operator_url),
        "{{JURISDICTION}}": _escape_markdown(settings.operator_jurisdiction),
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

    for md_file in sorted(LEGAL_DIR.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            meta, _ = parse_front_matter(content)

            slug = slug_from_filename(md_file.name)
            title = meta.get("title", slug.replace("-", " ").title())
            version = str(meta.get("version", "1.0.0"))
            eff_date_raw = meta.get("effective_date", "2026-04-20")
            try:
                effective_date = (
                    eff_date_raw
                    if isinstance(eff_date_raw, date)
                    else date.fromisoformat(str(eff_date_raw))
                )
            except (ValueError, TypeError):
                logger.warning("legal.invalid_date", slug=slug, raw=eff_date_raw)
                effective_date = datetime.now(tz=UTC).date()
            requires_acceptance = bool(meta.get("requires_acceptance", True))
            category = str(meta.get("category", "general"))
            display_order = int(meta.get("display_order", 0))
            file_path = f"legal/{md_file.name}"

            upsert_stmt = insert(LegalDocument).values(
                slug=slug,
                title=title,
                current_version=version,
                effective_date=effective_date,
                requires_acceptance=requires_acceptance,
                category=category,
                display_order=display_order,
                file_path=file_path,
            )
            upsert_stmt = upsert_stmt.on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "title": title,
                    "current_version": version,
                    "effective_date": effective_date,
                    "requires_acceptance": requires_acceptance,
                    "category": category,
                    "display_order": display_order,
                    "file_path": file_path,
                },
            )
            await session.execute(upsert_stmt)

            logger.info("legal.document_synced", slug=slug, version=version)

        except Exception:
            logger.exception("legal.sync_file_failed", file=md_file.name)

    await session.flush()

    result = await session.execute(select(LegalDocument).order_by(LegalDocument.display_order))
    return list(result.scalars().all())
