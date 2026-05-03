"""CRUD routes for long-lived API keys (gh#94).

Mounted at /api/v1/auth/api-keys. The plaintext token is returned
exactly once, in the response of POST /api-keys; subsequent reads
return only the prefix and metadata.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from engine.api.auth.api_keys import (
    VALID_SCOPES,
    issue_api_key,
)
from engine.api.auth.dependency import get_current_user
from engine.db.models import ApiKey, User
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(prefix="/auth/api-keys", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    expires_at: datetime | None = Field(default=None)
    env: str = Field(default="live", pattern=r"^[A-Za-z0-9_]+$", max_length=16)


class ApiKeySummary(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedResponse(ApiKeySummary):
    # Returned exactly once on POST. Surfaced to the operator and never
    # logged or stored in plaintext on the server side.
    token: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreatedResponse:
    invalid = [s for s in body.scopes if s.strip().lower() not in VALID_SCOPES]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scopes: {invalid}",
        )

    row, token = await issue_api_key(
        db,
        user_id=user.id,
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
        env=body.env,
    )
    await db.commit()
    return ApiKeyCreatedResponse(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes or []),
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        token=token,
    )


@router.get("", response_model=list[ApiKeySummary])
async def list_api_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeySummary]:
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    rows = result.scalars().all()
    return [
        ApiKeySummary(
            id=r.id,
            name=r.name,
            prefix=r.prefix,
            scopes=list(r.scopes or []),
            last_used_at=r.last_used_at,
            expires_at=r.expires_at,
            revoked_at=r.revoked_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(tz=UTC)
        await db.commit()
