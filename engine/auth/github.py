"""GitHub OAuth2 authentication provider.

This module implements the GitHub sign-in flow as discrete, independently
testable steps -- mirroring the structure of
:mod:`engine.auth.providers.google` -- and exposes them through a single
:class:`GitHubOAuthProvider` that satisfies the :class:`IOAuthProvider`
interface defined in :mod:`engine.auth.base`.

Unlike an OIDC provider, GitHub does **not** issue a signed ID token. An
access token is therefore validated by introspecting it against GitHub's
``/user`` API (:meth:`GitHubOAuthProvider.validate_access_token`). Because
GitHub returns a ``null`` ``email`` whenever the user has no public address
-- even with the ``user:email`` scope -- :meth:`validate_access_token`
transparently falls back to the ``/user/emails`` endpoint to resolve the
primary verified address.

Each failure mode raises a typed exception (all subclasses of
:class:`GitHubOAuthError`, and -- via the shared bases in
:mod:`engine.auth.base` -- also catchable as
:class:`engine.auth.base.TokenExchangeError` /
:class:`engine.auth.base.InvalidTokenError`) so callers can distinguish
network errors (:class:`TokenExchangeError`) from bad/expired tokens
(:class:`InvalidTokenError`) or simply catch the shared base to handle the
same failure family from any provider.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from engine.auth.base import (
    InvalidTokenError as _InvalidTokenErrorBase,
)
from engine.auth.base import (
    IOAuthProvider,
    OAuthError,
    TokenSet,
)
from engine.auth.base import (
    TokenExchangeError as _TokenExchangeErrorBase,
)
from engine.auth.base import (
    generate_state as _generate_state,
)
from engine.auth.base import (
    validate_state as _validate_state,
)

logger = structlog.get_logger()

# --- Public endpoints -------------------------------------------------------
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105  # endpoint URL, not a secret
_GITHUB_USER_URL = "https://api.github.com/user"
_GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

# HTTP status codes at or above this are error responses from a GitHub
# endpoint that must be surfaced as a typed provider exception.
_HTTP_ERROR_STATUS_THRESHOLD = 400

# Default scopes: ``read:user`` gives us the public profile; ``user:email``
# is required to resolve the (often private) primary email via /user/emails.
_DEFAULT_SCOPE = "read:user user:email"


# --- Exceptions -------------------------------------------------------------
class GitHubOAuthError(OAuthError):
    """Base class for every error raised by the GitHub OAuth2 provider."""


class TokenExchangeError(GitHubOAuthError, _TokenExchangeErrorBase):
    """Raised when the authorization-code -> token exchange fails (network
    error or non-2xx response from the token endpoint).

    Also subclasses the shared :class:`engine.auth.base.TokenExchangeError` so
    callers can catch that base to handle exchange failures from *any*
    provider.
    """


# Provider-specific alias used by the package-level registry/exports so the
# GitHub variant stays distinguishable from the Google one.
GitHubTokenExchangeError = TokenExchangeError


class InvalidTokenError(GitHubOAuthError, _InvalidTokenErrorBase):
    """Raised when an access token fails validation against GitHub's API
    (missing/empty token, HTTP 401, other HTTP error, or an incomplete
    profile payload).

    Also subclasses the shared :class:`engine.auth.base.InvalidTokenError` so
    callers can catch that base to handle invalid tokens from *any* provider.
    """


# Provider-specific alias used by the package-level registry/exports.
GitHubInvalidTokenError = InvalidTokenError


# --- Data classes -----------------------------------------------------------
@dataclass
class GitHubUserInfo:
    """Normalized, type-safe view of a validated GitHub user profile.

    GitHub's ``/user`` endpoint returns the numeric account ``id`` and the
    ``login`` (username) as the stable identifiers. ``email`` is frequently
    ``null`` (resolved separately from ``/user/emails``) and ``name`` is an
    optional display name.
    """

    id: str
    login: str
    email: str = ""
    name: str = ""
    avatar_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class GitHubOAuthProvider(IOAuthProvider):
    """GitHub OAuth2 provider.

    Parameters mirror the GitHub OAuth App configuration. For testability the
    HTTP ``transport`` is injectable so unit tests can stub GitHub's API with
    an :class:`httpx.MockTransport` and never touch the network.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._transport = transport

    @property
    def name(self) -> str:
        return "github"

    # -- IOAuthProvider: authorization URL + CSRF state ---------------------
    def get_authorize_url(
        self,
        *,
        state: str,
        scope: str = _DEFAULT_SCOPE,
    ) -> str:
        """Build the GitHub authorization endpoint URL.

        ``state`` is **required** -- it is the CSRF token. Issuing a URL
        without one would expose the callback to a login-CSRF attack.
        """
        if not state:
            raise GitHubOAuthError("state is required for CSRF protection")
        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": scope,
            "state": state,
        }
        return f"{_GITHUB_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def get_authorize_url_with_state(
        self,
        *,
        state: str = "",
        scope: str = _DEFAULT_SCOPE,
    ) -> tuple[str, str]:
        """Build the authorization URL and return the ``(url, state)`` pair.

        This is the canonical, typed accessor for the ``(url, state)`` tuple.
        A CSRF ``state`` token is **always** embedded: when the caller does not
        supply one, a cryptographically strong token is generated via
        :meth:`generate_state` so that no authorization URL is ever produced
        without CSRF protection. Returning the state alongside the URL lets the
        caller persist and later validate the exact value the IdP echoes back
        on the callback.

        Unlike :meth:`get_authorize_url` -- which returns only the URL string
        and *requires* a non-empty ``state`` -- this method auto-generates one
        when none is supplied, which is why it is the safer, self-documenting
        default for the ``state``-round-tripping authorize flow.
        """
        if not state:
            state = self.generate_state()
        url = self.get_authorize_url(state=state, scope=scope)
        return url, state

    @staticmethod
    def generate_state() -> str:
        return _generate_state()

    @staticmethod
    def validate_state(received_state: str | None, expected_state: str | None) -> None:
        try:
            _validate_state(received_state, expected_state)
        except OAuthError as exc:
            # Re-raise as the GitHub-typed error so callers only need to
            # catch GitHubOAuthError (or its base OAuthError).
            raise GitHubOAuthError(str(exc)) from exc

    # -- Authorization-code exchange ----------------------------------------
    async def exchange_code(self, code: str) -> TokenSet:
        """Exchange an authorization code for a :class:`TokenSet`.

        Any transport-level failure or non-2xx HTTP response from GitHub's
        token endpoint is wrapped in :class:`TokenExchangeError`.
        """
        if not code:
            raise TokenExchangeError("authorization code is required")

        data: dict[str, str] = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(
                    _GITHUB_TOKEN_URL,
                    data=data,
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            logger.warning("auth.github.token_exchange.network_error", error=str(exc))
            raise TokenExchangeError(
                f"network error contacting token endpoint: {exc}"
            ) from exc

        if response.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
            # Surface GitHub's structured error description for diagnostics.
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error") or body)
            except (ValueError, TypeError):
                detail = response.text
            logger.warning(
                "auth.github.token_exchange.http_error",
                status=response.status_code,
                detail=detail[:200],
            )
            raise TokenExchangeError(
                f"token endpoint returned HTTP {response.status_code}: {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenExchangeError("token endpoint returned non-JSON body") from exc

        if not payload or "access_token" not in payload:
            raise TokenExchangeError("token endpoint response missing access_token")

        logger.info("auth.github.token_exchange.success")
        return TokenSet(
            access_token=payload["access_token"],
            token_type=payload.get("token_type", "Bearer"),
            expires_in=payload.get("expires_in"),
            refresh_token=payload.get("refresh_token"),
            scope=payload.get("scope"),
            raw=payload,
        )

    # -- Access-token validation via /user ----------------------------------
    async def validate_access_token(self, access_token: str) -> GitHubUserInfo:
        """Validate an access token against GitHub's ``/user`` endpoint.

        The token is considered valid only if GitHub accepts it (HTTP 200) and
        returns a profile containing both an ``id`` and a ``login``. When the
        profile's ``email`` is absent (the common case for users with a
        private primary address), it is resolved from ``/user/emails``.

        Raises :class:`InvalidTokenError` for a missing/empty token, an HTTP
        401, any other HTTP error, a non-JSON body, or an incomplete profile.
        """
        if not access_token or not isinstance(access_token, str):
            raise InvalidTokenError("access token is required")

        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }

        profile: dict[str, Any]
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                user_resp = await client.get(_GITHUB_USER_URL, headers=headers)
                if user_resp.status_code == httpx.codes.UNAUTHORIZED:
                    raise InvalidTokenError("access token is invalid or expired")
                if user_resp.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
                    raise InvalidTokenError(
                        f"GitHub API returned HTTP {user_resp.status_code}"
                    )
                try:
                    profile = user_resp.json()
                except ValueError as exc:
                    raise InvalidTokenError(
                        "GitHub API returned non-JSON body"
                    ) from exc

                github_id = profile.get("id")
                login = profile.get("login")
                if github_id is None or not login:
                    raise InvalidTokenError(
                        "incomplete GitHub profile (missing id or login)"
                    )

                # GitHub returns a null email whenever no address is public,
                # even with the ``user:email`` scope. Resolve the primary
                # verified address from /user/emails in that case.
                email = profile.get("email")
                if not email:
                    email = await self._fetch_primary_email(client, access_token)
        except httpx.RequestError as exc:
            logger.warning("auth.github.validate_token.network_error", error=str(exc))
            raise InvalidTokenError(
                f"network error contacting GitHub API: {exc}"
            ) from exc

        resolved_email = email or f"{login}@users.noreply.github.com"
        return GitHubUserInfo(
            id=str(github_id),
            login=str(login),
            email=resolved_email,
            name=str(profile.get("name") or login),
            avatar_url=profile.get("avatar_url"),
            raw=profile,
        )

    async def _fetch_primary_email(
        self, client: httpx.AsyncClient, access_token: str
    ) -> str | None:
        """Resolve the primary verified email from GitHub's ``/user/emails``.

        Best-effort: any network/parse failure returns ``None`` so that
        :meth:`validate_access_token` can fall back to a synthesized noreply
        address rather than rejecting an otherwise-valid user.
        """
        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            resp = await client.get(_GITHUB_EMAILS_URL, headers=headers)
        except httpx.RequestError:
            return None
        if resp.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
            return None
        try:
            emails = resp.json()
        except (ValueError, TypeError):
            return None
        if not isinstance(emails, list):
            return None

        def _pick(predicate) -> str | None:
            for entry in emails:
                if isinstance(entry, dict) and predicate(entry):
                    addr = entry.get("email")
                    if isinstance(addr, str) and addr:
                        return addr
            return None

        # Prefer the primary verified address, then any verified address.
        return _pick(lambda e: e.get("primary") and e.get("verified")) or _pick(
            lambda e: e.get("verified")
        )

    # -- User / email mapping ----------------------------------------------
    def map_user(self, info: GitHubUserInfo) -> dict[str, Any]:
        """Map a validated GitHub profile to the Nexus user-model shape.

        Returns a plain dict with the fields the rest of the engine expects
        when creating/linking an OAuth-backed user: ``external_id`` (the
        stable GitHub account id), ``provider``, ``email``, ``display_name``
        and the default ``roles`` list.
        """
        return {
            "external_id": info.id,
            "provider": self.name,
            "email": info.email,
            "display_name": info.name or info.login,
            "roles": ["user"],
        }
