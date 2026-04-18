from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import Depends, HTTPException

from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.db.models import LegalDocument, User

logger = structlog.get_logger()

_AUTH_NOT_WIRED = (
    "Legal consent enforcement requires an authenticated user. "
    "Wire get_current_user (ADR-0002) into this dependency before shipping. "
    "See SEV-206 / gh#154 for tracking."
)


async def _placeholder_get_current_user() -> None:
    """Placeholder until ADR-0002 (auth) lands.

    Returns None so that ``require_legal_acceptance`` raises 401,
    making the auth gap visible rather than silently bypassing consent.
    Remove this once get_current_user is implemented.
    """
    logger.warning("legal.auth_not_wired")


async def require_legal_acceptance(
    user: User | None = Depends(_placeholder_get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User | None:
    if user is None:
        raise HTTPException(status_code=401, detail=_AUTH_NOT_WIRED)

    from engine.legal.service import get_pending_acceptances  # noqa: PLC0415

    pending: list[LegalDocument] = await get_pending_acceptances(db, user.id)
    if pending:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
    return user
