from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update

from engine.api.auth.base import UserInfo
from engine.api.auth.dependency import get_current_user
from engine.api.auth.jwt import (
    create_access_token,
    generate_refresh_token,
    get_refresh_token_expiry,
    hash_token,
)
from engine.api.auth.registry import AuthProviderRegistry
from engine.config import settings
from engine.db.models import RefreshToken, User
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

router = APIRouter()

MIN_PASSWORD_LENGTH = 8


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserProfileResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    email: str
    display_name: str
    role: str
    auth_provider: str
    is_active: bool


def _get_registry(request: Request) -> AuthProviderRegistry:
    return request.app.state.auth_registry


def _mint_tokens(user: User) -> tuple[str, str]:
    access_token = create_access_token(
        sub=str(user.id),
        email=user.email,
        role=user.role,
        provider=user.auth_provider,
    )
    raw_refresh = generate_refresh_token()
    return access_token, raw_refresh


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _store_refresh_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    raw_token: str,
    request: Request | None = None,
) -> None:
    token_record = RefreshToken(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=get_refresh_token_expiry(),
        user_agent=request.headers.get("user-agent") if request else None,
        ip_address=request.client.host if request and request.client else None,
    )
    db.add(token_record)
    await db.flush()


def _build_token_response(access_token: str, raw_refresh: str) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    registry = _get_registry(request)
    local_provider = registry.get("local")
    if local_provider is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Local registration not available"
        )

    user_info = UserInfo(
        email=req.email,
        display_name=req.display_name or req.email.split("@")[0],
        provider="local",
    )
    result = await local_provider.create_user(user_info=user_info, password=req.password, db=db)
    if not result.success:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.error)

    db_result = await db.execute(select(User).where(User.email == req.email))
    user = db_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="User creation failed"
        )

    access_token, raw_refresh = _mint_tokens(user)
    await _store_refresh_token(db, user.id, raw_refresh, request)
    return _build_token_response(access_token, raw_refresh)


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    registry = _get_registry(request)
    result = await registry.authenticate("local", email=req.email, password=req.password, db=db)
    if not result.success:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.error)

    db_result = await db.execute(select(User).where(User.email == req.email))
    user = db_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access_token, raw_refresh = _mint_tokens(user)
    await _store_refresh_token(db, user.id, raw_refresh, request)
    logger.info("auth.login_success", user_id=str(user.id), provider="local")
    return _build_token_response(access_token, raw_refresh)


@router.post("/refresh")
async def refresh_token(
    req: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    token_hash_val = hash_token(req.refresh_token)
    now = datetime.now(tz=UTC)

    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash_val).with_for_update()
    )
    stored_token = result.scalar_one_or_none()

    if stored_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    if stored_token.revoked_at is not None:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == stored_token.user_id)
            .values(revoked_at=now)
        )
        await db.flush()
        logger.warning("auth.token_replay_detected", user_id=str(stored_token.user_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token reuse detected — all sessions revoked",
        )

    if _aware(stored_token.expires_at) < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired"
        )

    stored_token.revoked_at = now
    await db.flush()

    user = await db.get(User, stored_token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or disabled"
        )

    access_token, raw_refresh = _mint_tokens(user)
    await _store_refresh_token(db, user.id, raw_refresh, request)
    return _build_token_response(access_token, raw_refresh)


@router.get("/me", response_model=UserProfileResponse)
async def get_me(user: User = Depends(get_current_user)) -> UserProfileResponse:
    return UserProfileResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        auth_provider=user.auth_provider,
        is_active=user.is_active,
    )


@router.post("/logout")
async def logout(
    req: RefreshRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    now = datetime.now(tz=UTC)
    if req and req.refresh_token:
        token_hash_val = hash_token(req.refresh_token)
        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash_val,
                RefreshToken.user_id == user.id,
            )
        )
        token = result.scalar_one_or_none()
        if token:
            token.revoked_at = now
    else:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    logger.info("auth.logout", user_id=str(user.id))
    return {"status": "logged_out"}


@router.get("/{provider}/authorize")
async def authorize_provider(provider: str, request: Request) -> dict[str, str]:
    registry = _get_registry(request)
    auth_provider = registry.get(provider)
    if auth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider}' not configured"
        )

    state = secrets.token_urlsafe(32)

    url = ""
    if hasattr(auth_provider, "get_authorize_url"):
        maybe_url = auth_provider.get_authorize_url(state=state)
        if callable(maybe_url) and not isinstance(maybe_url, str):
            maybe_url = await maybe_url
        url = maybe_url

    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not build authorize URL",
        )

    return {"authorize_url": str(url), "state": state}


@router.get("/{provider}/callback")
async def provider_callback(
    provider: str,
    code: str,
    state: str = "",
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    if state and request is not None:
        cookie_state = request.cookies.get(f"oauth_state_{provider}")
        if not cookie_state or not secrets.compare_digest(cookie_state, state):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Invalid OAuth state parameter"
            )

    registry = _get_registry(request)  # type: ignore[arg-type]
    result = await registry.authenticate(provider, code=code, db=db)
    if not result.success:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.error)

    if result.user_info is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication succeeded but no user info",
        )

    db_result = await db.execute(
        select(User).where(
            User.auth_provider == provider,
            User.external_id == result.user_info.external_id,
        )
    )
    user = db_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="User not found after auth"
        )

    access_token, raw_refresh = _mint_tokens(user)
    await _store_refresh_token(db, user.id, raw_refresh, request)
    logger.info("auth.oauth_callback_success", user_id=str(user.id), provider=provider)
    return _build_token_response(access_token, raw_refresh)
