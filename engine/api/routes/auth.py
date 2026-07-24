from __future__ import annotations

import inspect
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
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


class MFARequiredResponse(BaseModel):
    mfa_required: bool = True
    challenge_token: str


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    registry = _get_registry(request)
    result = await registry.authenticate("local", email=req.email, password=req.password, db=db)
    if not result.success:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.error)

    db_result = await db.execute(select(User).where(User.email == req.email))
    user = db_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.mfa_enabled and user.mfa_secret_encrypted:
        from engine.api.auth.mfa_service import issue_challenge

        challenge = issue_challenge(str(user.id))
        logger.info("auth.login_mfa_required", user_id=str(user.id))
        return MFARequiredResponse(challenge_token=challenge).model_dump()

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
    atomic_result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash_val, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(
            RefreshToken.id, RefreshToken.user_id, RefreshToken.expires_at, RefreshToken.revoked_at
        )
    )
    rotated_row = atomic_result.first()

    if rotated_row is None:
        stale_result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash_val)
        )
        stale_token = stale_result.scalar_one_or_none()
        if stale_token is not None and stale_token.revoked_at is not None:
            await db.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.user_id == stale_token.user_id, RefreshToken.revoked_at.is_(None)
                )
                .values(revoked_at=now)
            )
            logger.warning("auth.token_replay_detected", user_id=str(stale_token.user_id))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token reuse detected — all sessions revoked",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    await db.flush()

    expires_at = rotated_row.expires_at
    if _aware(expires_at) < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired"
        )

    user = await db.get(User, rotated_row.user_id)
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


# C0 (\x00-\x1f) and C1 (\x7f-\x9f) control characters. These let an
# attacker smuggle extra HTTP headers / cookie content into the
# ``authorize_url`` and ``state`` the route surfaces to the browser (response
# splitting / cookie injection), so any value carrying them is rejected.
_OAUTH_VALUE_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def validate_authorize_url(url: str) -> str:
    """Defence-in-depth validation of an authorize URL before it is surfaced.

    The route returns the provider-built authorize URL verbatim in the JSON
    body (``{"authorize_url": url}``); a misbehaving or hostile provider --
    or a monkey-patched/legacy one returning a malformed value -- could
    otherwise hand back a URL that carries CRLF / control characters (a
    response-splitting / header-injection vector when the URL is ever used in
    a ``Location`` redirect) or a non-HTTPS scheme (the authorization
    response / ``redirect_uri`` carries sensitive material that an observer
    must never see).

    The HTTPS-scheme check runs first; control-character rejection runs after
    it, so a URL can never reach the browser carrying smuggled CRLF even when
    its scheme is valid. Raises :class:`HTTPException` (HTTP 500, generic
    message) on any failure -- never reflecting the hostile value back.
    """
    if not isinstance(url, str) or not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not build authorize URL",
        )
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() != "https":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authorize URL must use the https scheme",
        )
    if _OAUTH_VALUE_CONTROL_CHARS_RE.search(url):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authorize URL contains control characters",
        )
    return url


def validate_oauth_state(state: str) -> str:
    """Defence-in-depth validation of the OAuth ``state`` token.

    The route mints its own ``state`` (``secrets.token_urlsafe(32)``), but a
    provider MAY return an AUTHORITATIVE value of its own (the one embedded in
    the URL it built). That returned value is persisted in the
    ``oauth_state_{provider}`` session cookie AND echoed by the IdP on the
    callback, where it is compared with :func:`secrets.compare_digest`. It
    MUST therefore be a non-empty, control-character-free string:

    * an empty value would let the callback's ``compare_digest`` silently
      pass against a missing/empty cookie (defeating CSRF protection); and
    * a CRLF-laden value would let a hostile provider inject cookie / header
      content into the ``Set-Cookie`` the route emits.

    This guard rejects both. It runs on every URL-building branch so a
    provider can never smuggle a bad state regardless of which accessor it
    implements. Raises :class:`HTTPException` (HTTP 500, generic message) on
    any failure -- never reflecting the hostile value back.
    """
    if not isinstance(state, str) or not state:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth state is required",
        )
    if _OAUTH_VALUE_CONTROL_CHARS_RE.search(state):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth state contains control characters",
        )
    return state


@router.get("/{provider}/authorize")
async def authorize_provider(
    provider: str, request: Request, response: Response
) -> dict[str, str]:
    registry = _get_registry(request)
    auth_provider = registry.get(provider)
    if auth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider}' not configured"
        )

    state = secrets.token_urlsafe(32)

    url = ""
    # Prefer the typed ``(url, state)`` accessor when the provider offers it
    # (GitHub); otherwise fall back to the generic ``get_authorize_url``
    # (Google/OIDC return a plain URL string, though a tuple is also handled).
    # When a tuple is returned we persist the AUTHORITATIVE ``state`` embedded
    # in the URL -- the value the IdP echoes back on the callback -- rather
    # than the locally minted token, so the session cookie validated on the
    # callback always matches what the user's browser actually round-trips.
    # This defends against a provider that ignores the supplied state and mints
    # its own: the cookie would otherwise hold a value the IdP never echoes.
    if hasattr(auth_provider, "get_authorize_url_with_state"):
        result = auth_provider.get_authorize_url_with_state(state=state)
        # A provider MAY implement ``get_authorize_url_with_state`` as a
        # coroutine (the registry is provider-agnostic), so -- mirroring the
        # generic fallback path below -- await it only when it is actually
        # awaitable. A synchronous ``(url, state)`` tuple is unpacked directly.
        if inspect.isawaitable(result):
            result = await result
        url, state = result
    elif hasattr(auth_provider, "get_authorize_url"):
        maybe_url = auth_provider.get_authorize_url(state=state)
        if inspect.isawaitable(maybe_url):
            maybe_url = await maybe_url
        if isinstance(maybe_url, tuple):
            url, state = maybe_url
        else:
            url = maybe_url

    # Defence in depth: regardless of which branch produced them, the URL and
    # state we surface to the browser (and persist in the session cookie) MUST
    # be sane. State validation is extracted here so it runs on EVERY branch --
    # including the plain-string ``else`` fallback -- rather than only on the
    # tuple-returning paths, so a provider can never smuggle CRLF or hand back
    # a non-HTTPS URL / empty state via any accessor. This single validation
    # point supersedes the previous ``if not url`` check (now folded into
    # :func:`validate_authorize_url`).
    url = validate_authorize_url(url)
    state = validate_oauth_state(state)

    response.set_cookie(
        key=f"oauth_state_{provider}",
        value=state,
        httponly=True,
        max_age=600,
        samesite="lax",
        secure=settings.is_production,
        path="/api/v1/auth",
    )

    return {"authorize_url": str(url), "state": state}


@router.get("/{provider}/callback")
async def provider_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    if not state:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing OAuth state parameter",
        )

    cookie_state = request.cookies.get(f"oauth_state_{provider}")
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing OAuth state parameter",
        )

    response.delete_cookie(
        key=f"oauth_state_{provider}",
        path="/api/v1/auth",
    )

    registry = _get_registry(request)
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
