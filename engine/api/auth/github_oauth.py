"""GitHub OAuth2 adapter that bridges :mod:`engine.auth.github` to the
FastAPI :class:`~engine.api.auth.registry.AuthProviderRegistry`.

The *library* layer (:class:`engine.auth.github.GitHubOAuthProvider`) owns the
pure OAuth2 mechanics -- authorization-URL generation, the authorization-code
-> access-token exchange, and access-token validation against GitHub's
``/user`` API (including the ``/user/emails`` fallback for users with a
private primary address). That layer is fully unit-testable with an
:class:`httpx.MockTransport` and never touches the database.

This module is the thin *integration* layer: it implements the
:class:`~engine.api.auth.base.IAuthProvider` contract the
``AuthProviderRegistry`` (and therefore the ``/auth/{provider}/callback``
route in :mod:`engine.api.routes.auth`) expects. It delegates the networked
OAuth2 steps to :class:`GitHubOAuthProvider` and is responsible only for the
domain concerns a registry adapter must own:

* looking up an existing Nexus user by ``(provider, external_id)``,
* guarding against email re-use across providers,
* creating a new :class:`~engine.db.models.User` on first GitHub sign-in,
* rejecting disabled accounts, and
* mapping the validated GitHub profile onto :class:`UserInfo`.

All OAuth2 transport failures surface as :class:`AuthResult` failures with a
descriptive, non-sensitive ``error`` message rather than being swallowed by a
bare ``except``.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.auth.github import (
    GitHubOAuthError,
    GitHubOAuthProvider,
    GitHubUserInfo,
    InvalidTokenError,
    TokenExchangeError,
)
from engine.config import settings
from engine.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# Default scopes requested on the authorization endpoint. ``read:user`` yields
# the public profile; ``user:email`` is required to resolve the (frequently
# private) primary email via /user/emails.
_DEFAULT_SCOPE = "read:user user:email"


class GitHubAuthProvider(IAuthProvider):
    """Registry-facing GitHub provider.

    Parameters
    ----------
    oauth_provider:
        Optional pre-built :class:`GitHubOAuthProvider`. Production leaves this
        ``None`` so it is lazily constructed from ``settings``; tests inject a
        stub (or a real provider backed by an :class:`httpx.MockTransport`) so
        no network access is required.
    """

    def __init__(self, oauth_provider: GitHubOAuthProvider | None = None) -> None:
        self._oauth = oauth_provider

    @property
    def name(self) -> str:
        return "github"

    def _get_oauth(self) -> GitHubOAuthProvider:
        """Lazily build a :class:`GitHubOAuthProvider` wired to app settings.

        Settings are read lazily (not in ``__init__``) so this adapter can be
        instantiated unconditionally during registry construction even when
        GitHub is not configured; it only fails when actually *used*.
        """
        if self._oauth is None:
            self._oauth = GitHubOAuthProvider(
                client_id=settings.github_client_id,
                client_secret=settings.github_client_secret,
                redirect_uri=settings.github_redirect_uri,
            )
        return self._oauth

    def get_authorize_url(self, state: str = "") -> str:
        """Build the GitHub authorization endpoint URL.

        A CSRF ``state`` token is **always** embedded. When the caller does not
        supply one, a cryptographically strong token is generated via
        :meth:`GitHubOAuthProvider.generate_state` so that no authorization URL
        is ever produced without CSRF protection -- the previous behaviour of
        silently omitting ``state`` when none was passed exposed the callback to
        a login-CSRF attack. Callers that round-trip the token (the normal
        case -- the auth route issues one and validates it on the callback)
        should supply their own ``state`` and persist it server-side (e.g. in a
        signed cookie).
        """
        if not state:
            state = self._get_oauth().generate_state()
        return self._get_oauth().get_authorize_url(state=state, scope=_DEFAULT_SCOPE)

    async def _resolve_profile(
        self, oauth: GitHubOAuthProvider, code: str
    ) -> GitHubUserInfo | AuthResult:
        """Exchange ``code`` for an access token, then validate it.

        Returns the validated :class:`GitHubUserInfo` on success, or an
        unsuccessful :class:`AuthResult` capturing any OAuth2 transport
        failure so the caller can short-circuit uniformly.
        """
        try:
            token_set = await oauth.exchange_code(code)
            return await oauth.validate_access_token(token_set.access_token)
        except TokenExchangeError as exc:
            logger.warning("auth.github.token_exchange_failed", error=str(exc))
            return AuthResult(success=False, error="GitHub token exchange failed")
        except InvalidTokenError as exc:
            logger.warning("auth.github.invalid_token", error=str(exc))
            return AuthResult(success=False, error="GitHub authentication failed: invalid token")
        except GitHubOAuthError as exc:
            logger.warning("auth.github.oauth_error", error=str(exc))
            return AuthResult(success=False, error="GitHub authentication failed")

    async def _get_or_create_user(
        self, db: AsyncSession, info: GitHubUserInfo
    ) -> User | AuthResult:
        """Resolve an existing Nexus user for ``info`` or create a new one.

        Returns the persisted :class:`User` on success, or an unsuccessful
        :class:`AuthResult` describing why the profile could not be linked to
        an account (incomplete profile, missing email, email re-use across
        providers, or a disabled account) so the caller can short-circuit.
        """
        github_id = info.id
        email = info.email
        name = info.name or info.login or "GitHub User"

        if not github_id:
            return AuthResult(success=False, error="Incomplete GitHub profile")

        # --- existing user linked to this GitHub identity? ------------------
        result = await db.execute(
            select(User).where(User.auth_provider == "github", User.external_id == github_id)
        )
        user = result.scalar_one_or_none()

        # --- first sign-in -> create a new Nexus user -----------------------
        if user is None:
            if not email:
                # The provider synthesizes a noreply address for users with no
                # public email, so an empty value here means the profile is
                # unusable. Never persist a user with a NULL/blank email: the
                # ``User.email`` column is UNIQUE NOT NULL, and accepting a
                # blank address would let an attacker create unidentifiable,
                # colliding accounts.
                logger.warning("auth.github.missing_email", github_id=github_id)
                return AuthResult(
                    success=False, error="GitHub profile did not provide an email address"
                )
            existing = await db.execute(select(User).where(User.email == email))
            existing_user = existing.scalar_one_or_none()
            if existing_user is not None:
                return AuthResult(
                    success=False, error="Email already registered with a different provider"
                )

            user = User(
                email=email,
                hashed_password=None,
                display_name=name,
                is_active=True,
                role="user",
                auth_provider="github",
                external_id=github_id,
            )
            db.add(user)
            await db.flush()
            await db.refresh(user)
            logger.info("auth.github.user_created", user_id=str(user.id))

        if not user.is_active:
            return AuthResult(success=False, error="Account is disabled")

        return user

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        """Run the GitHub authorization-code flow end to end.

        Expects ``code`` (the authorization code returned to the callback) and
        ``db`` (an async session). Performs:

        1. ``code`` -> ``access_token`` via :meth:`GitHubOAuthProvider.exchange_code`
        2. ``access_token`` -> validated :class:`GitHubUserInfo` via
           :meth:`GitHubOAuthProvider.validate_access_token`
        3. User lookup / creation against the Nexus ``User`` model.

        Any OAuth2 failure (network error, bad code, expired/revoked token,
        incomplete profile) returns an unsuccessful :class:`AuthResult` with a
        descriptive message -- it is **not** raised, because the registry
        contract models auth outcomes as ``AuthResult`` values.
        """
        code = kwargs.get("code")
        db: AsyncSession | None = kwargs.get("db")
        state = kwargs.get("state")
        expected_state = kwargs.get("expected_state")
        if not code or db is None:
            return AuthResult(success=False, error="Authorization code and db session required")

        # Defense-in-depth CSRF check. The HTTP callback route validates the
        # ``state`` echoed by GitHub against a signed cookie itself, but making
        # the comparison available here keeps the adapter secure when used as a
        # standalone API: when the caller supplies the ``state`` it originally
        # issued (``expected_state``), the value returned by GitHub
        # (``state``) MUST match it in constant time via
        # :func:`hmac.compare_digest` to avoid a timing oracle.
        if expected_state is not None and (
            not isinstance(state, str)
            or not isinstance(expected_state, str)
            or not state
            or not expected_state
            or not hmac.compare_digest(state, expected_state)
        ):
            logger.warning("auth.github.state_validation_failed")
            return AuthResult(success=False, error="CSRF state validation failed")

        # --- 1 + 2: authorization-code -> access-token -> validated profile ---
        resolved = await self._resolve_profile(self._get_oauth(), code)
        if isinstance(resolved, AuthResult):
            return resolved
        info: GitHubUserInfo = resolved

        # --- 3: user lookup / creation against the Nexus ``User`` model -----
        outcome = await self._get_or_create_user(db, info)
        if isinstance(outcome, AuthResult):
            return outcome
        user: User = outcome

        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=info.id,
                email=user.email,
                display_name=user.display_name,
                provider="github",
                roles=[user.role],
            ),
        )
