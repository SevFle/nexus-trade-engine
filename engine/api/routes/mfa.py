"""MFA enrollment / verification routes (gh#126)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from engine.api.auth.dependency import get_current_user
from engine.api.auth.local import _verify_password
from engine.api.auth.mfa_service import (
    MFAServiceError,
    begin_enrollment,
    confirm_enrollment,
    generate_backup_codes,
    hash_backup_codes,
    issue_challenge,
    verify_challenge,
    verify_login_code,
)
from engine.api.routes.auth import _build_token_response, _mint_tokens, _store_refresh_token
from engine.db.models import User
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

router = APIRouter()


class EnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class ConfirmRequest(BaseModel):
    secret: str
    code: str


class ConfirmResponse(BaseModel):
    backup_codes: list[str]


class VerifyRequest(BaseModel):
    challenge_token: str
    code: str


class DisableRequest(BaseModel):
    password: str
    code: str


class RegenBackupCodesRequest(BaseModel):
    code: str


@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    user: User = Depends(get_current_user),
) -> EnrollResponse:
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="MFA already enabled"
        )
    try:
        artifact = begin_enrollment(account=user.email)
    except MFAServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return EnrollResponse(secret=artifact.secret_b32, otpauth_uri=artifact.otpauth_uri)


@router.post("/enroll/confirm", response_model=ConfirmResponse)
async def confirm(
    body: ConfirmRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConfirmResponse:
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="MFA already enabled"
        )
    try:
        confirmed = confirm_enrollment(secret_b32=body.secret, code=body.code)
    except MFAServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    user.mfa_enabled = True
    user.mfa_secret_encrypted = confirmed.encrypted_secret
    user.mfa_backup_codes = confirmed.backup_codes_storage
    db.add(user)
    await db.flush()
    logger.info("auth.mfa_enrolled", user_id=str(user.id))
    return ConfirmResponse(backup_codes=confirmed.backup_codes_plaintext)


@router.post("/verify")
async def verify(
    body: VerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        user_id = verify_challenge(body.challenge_token)
    except MFAServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or disabled"
        )
    if not user.mfa_enabled or not user.mfa_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled for user"
        )

    try:
        ok, new_codes = verify_login_code(
            encrypted_secret=user.mfa_secret_encrypted,
            code=body.code,
            backup_codes=user.mfa_backup_codes,
        )
    except MFAServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    if not ok:
        logger.warning("auth.mfa_verify_failed", user_id=str(user.id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA code"
        )

    if new_codes is not None:
        user.mfa_backup_codes = new_codes
        db.add(user)
        await db.flush()

    access_token, raw_refresh = _mint_tokens(user)
    await _store_refresh_token(db, user.id, raw_refresh, request)
    logger.info("auth.mfa_verify_success", user_id=str(user.id))
    return _build_token_response(access_token, raw_refresh).model_dump()


@router.post("/disable")
async def disable(
    body: DisableRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    if not user.mfa_enabled or not user.mfa_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA not enabled"
        )
    if not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot verify password for non-local user",
        )
    if not _verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password"
        )
    try:
        ok, _ = verify_login_code(
            encrypted_secret=user.mfa_secret_encrypted,
            code=body.code,
            backup_codes=user.mfa_backup_codes,
        )
    except MFAServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA code"
        )
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_backup_codes = None
    db.add(user)
    await db.flush()
    logger.info("auth.mfa_disabled", user_id=str(user.id))
    return {"status": "disabled"}


@router.post("/backup-codes/regen", response_model=ConfirmResponse)
async def regen_backup_codes(
    body: RegenBackupCodesRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConfirmResponse:
    if not user.mfa_enabled or not user.mfa_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA not enabled"
        )
    try:
        ok, _ = verify_login_code(
            encrypted_secret=user.mfa_secret_encrypted,
            code=body.code,
            backup_codes=user.mfa_backup_codes,
        )
    except MFAServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA code"
        )
    plaintext = generate_backup_codes()
    user.mfa_backup_codes = hash_backup_codes(plaintext)
    db.add(user)
    await db.flush()
    logger.info("auth.mfa_backup_codes_regen", user_id=str(user.id))
    return ConfirmResponse(backup_codes=plaintext)


__all__ = ["issue_challenge", "router"]
