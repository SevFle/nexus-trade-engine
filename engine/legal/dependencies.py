from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException

from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

# TODO(auth): Replace with Depends(get_current_user) once auth module lands.
#     Consent enforcement is a no-op until then — every request passes through.
#     See ADR-0005 (auth) and SEV-501 B2.
_placeholder_user_id: uuid.UUID | None = None


async def require_legal_acceptance(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    if _placeholder_user_id is None:
        return
    pending = await legal_service.get_pending_acceptances(db, _placeholder_user_id)
    if pending:
        raise HTTPException(
            status_code=451,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
