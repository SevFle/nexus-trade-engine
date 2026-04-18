from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException

from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.db.models import LegalDocument, User


async def require_legal_acceptance(
    user: User | None = None,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User | None:
    if user is None:
        return None

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
