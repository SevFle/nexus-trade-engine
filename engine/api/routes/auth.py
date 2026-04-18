from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from engine.api.auth.dependency import (
    generate_refresh_token,
    get_current_user,
    revoke_all_user_tokens,
    revoke_refresh_token,
    store_refresh_token,
    verify_and_rotate_refresh_token,
)
from engine.api.auth.jwt import create_access_token
from engine.api.auth.local import LocalAuthProvider
from engine.config import settings
from engine.db.models import User
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

router = APIRouter()

_REFRESH_COOKIE_NAME = "nexus_refresh_token"
_ACCESS_EXPIRE_SECONDS = settings.jwt_access_token_expire_minutes * 60


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str | None = None
    everywhere: bool = False


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = _ACCESS_EXPIRE_SECONDS


class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    auth_provider: str

    class Config:
        from_attributes = True


def _build_token_response(user: User, refresh_plain: str) -> TokenResponse:
    access = create_access_token(
        user_id=str(user.id),
        email=user.email,
        role=user.role,
        provider=user.auth_provider,
    )
    return TokenResponse(access_token=access, refresh_token=refresh_plain)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    provider = LocalAuthProvider()
    result = await provider.register_user(
        email=req.email,
        password=req.password,
        display_name=req.display_name,
        db=db,
    )

    if not result.success:
        if "already exists" in (result.error or ""):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.error)
        if "disabled" in (result.error or ""):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result.error)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.error)

    user_info = result.user_info
    assert user_info is not None

    result2 = await db.execute(select(User).where(User.email == req.email))
    user = result2.scalar_one()

    refresh_plain = generate_refresh_token()
    await store_refresh_token(db, user.id, refresh_plain)
    return _build_token_response(user, refresh_plain)


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    provider = LocalAuthProvider()
    result = await provider.authenticate_login(
        email=req.email,
        password=req.password,
        db=db,
    )

    if not result.success:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user_info = result.user_info
    assert user_info is not None

    result2 = await db.execute(select(User).where(User.email == req.email))
    user = result2.scalar_one()

    refresh_plain = generate_refresh_token()
    await store_refresh_token(
        db,
        user.id,
        refresh_plain,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    return _build_token_response(user, refresh_plain)


@router.post("/refresh")
async def refresh_token(
    req: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    plain_token = req.refresh_token
    if not plain_token:
        plain_token = request.cookies.get(_REFRESH_COOKIE_NAME)

    if not plain_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token required"
        )

    user, new_refresh = await verify_and_rotate_refresh_token(
        db,
        plain_token,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )

    return _build_token_response(user, new_refresh)


@router.get("/me", response_model=UserProfile)
async def me(user: User = Depends(get_current_user)) -> UserProfile:
    return UserProfile(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        auth_provider=user.auth_provider,
    )


@router.post("/logout")
async def logout(
    req: LogoutRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    if req.everywhere:
        await revoke_all_user_tokens(db, user.id)
    elif req.refresh_token:
        await revoke_refresh_token(db, req.refresh_token)

    return {"status": "logged_out"}


@router.get("/{provider}/authorize")
async def authorize_provider(provider: str):
    from engine.api.auth.github_oauth import GitHubAuthProvider
    from engine.api.auth.google import GoogleAuthProvider
    from engine.api.auth.oidc import OIDCAuthProvider

    state = secrets.token_urlsafe(32)

    if provider == "google":
        p = GoogleAuthProvider()
        url = p.get_authorize_url(state)
    elif provider == "github":
        p = GitHubAuthProvider()
        url = p.get_authorize_url(state)
    elif provider == "oidc":
        p = OIDCAuthProvider()
        discovery = await p._get_discovery()
        p._discovery_doc = discovery
        url = p.get_authorize_url(state)
        if url is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="OIDC not configured"
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown provider: {provider}"
        )

    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=url)


@router.get("/{provider}/callback")
async def provider_callback(
    provider: str,
    code: str,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:

    registry = _get_registry()
    result = await registry.authenticate(provider, code=code, db=db)

    if not result.success:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.error)

    user_info = result.user_info
    assert user_info is not None

    result2 = await db.execute(
        select(User).where(
            User.auth_provider == provider,
            User.external_id == user_info.external_id,
        )
    )
    user = result2.scalar_one_or_none()
    if user is None:
        result2 = await db.execute(select(User).where(User.email == user_info.email))
        user = result2.scalar_one_or_none()

    assert user is not None

    refresh_plain = generate_refresh_token()
    await store_refresh_token(db, user.id, refresh_plain)
    return _build_token_response(user, refresh_plain)


def _get_registry():
    from engine.api.auth.github_oauth import GitHubAuthProvider
    from engine.api.auth.google import GoogleAuthProvider
    from engine.api.auth.ldap import LDAPAuthProvider
    from engine.api.auth.local import LocalAuthProvider
    from engine.api.auth.oidc import OIDCAuthProvider
    from engine.api.auth.registry import AuthProviderRegistry

    registry = AuthProviderRegistry()
    for name in settings.enabled_providers:
        if name == "local":
            registry.register(LocalAuthProvider())
        elif name == "google":
            registry.register(GoogleAuthProvider())
        elif name == "github":
            registry.register(GitHubAuthProvider())
        elif name == "oidc":
            registry.register(OIDCAuthProvider())
        elif name == "ldap":
            registry.register(LDAPAuthProvider())
    return registry
