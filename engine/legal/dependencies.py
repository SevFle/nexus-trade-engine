from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException

from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_placeholder_uuid = uuid.UUID("00000000-0000-0000-0000-000000000000")


async def require_legal_acceptance(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    pending = await legal_service.get_pending_acceptances(db, _placeholder_uuid)
    if pending:
        raise HTTPException(
            status_code=451,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
