"""Authentication providers package.

This package hosts well-decomposed, protocol-aware identity providers
(OAuth2 / OIDC). It complements :mod:`engine.api.auth` (which holds the
FastAPI integration, base classes, and the provider registry) with providers
that split each OAuth2 flow into its discrete steps so they can be tested
independently.

All concrete providers satisfy the :class:`IOAuthProvider` interface declared
in :mod:`engine.auth.base`. Use :func:`get_oauth_provider` to resolve a
provider by name from the application configuration.
"""

from __future__ import annotations

from engine.auth.base import (
    IOAuthProvider,
    OAuthError,
    TokenSet,
    generate_state,
    validate_state,
)
from engine.auth.github import (
    GitHubOAuthError,
    GitHubOAuthProvider,
    GitHubUserInfo,
    InvalidTokenError,
    TokenExchangeError,
)
from engine.auth.providers.google import (
    GoogleOAuthError,
    GoogleOAuthProvider,
    IDTokenClaims,
)
from engine.auth.providers.google import (
    InvalidTokenError as GoogleInvalidTokenError,
)
from engine.auth.providers.google import (
    TokenExchangeError as GoogleTokenExchangeError,
)
from engine.auth.providers.google import (
    TokenSet as GoogleTokenSet,
)

__all__ = [
    "GitHubOAuthError",
    "GitHubOAuthProvider",
    "GitHubUserInfo",
    "GoogleInvalidTokenError",
    "GoogleOAuthError",
    "GoogleOAuthProvider",
    "GoogleTokenExchangeError",
    "GoogleTokenSet",
    "IDTokenClaims",
    "IOAuthProvider",
    "InvalidTokenError",
    "OAuthError",
    "TokenExchangeError",
    "TokenSet",
    "generate_state",
    "get_oauth_provider",
    "validate_state",
]


def get_oauth_provider(name: str) -> IOAuthProvider | None:
    """Build a configured OAuth2 provider by name from app settings.

    This is the package's provider factory/registry entry point: given a
    provider name (``"github"``, ``"google"``) it constructs a provider wired
    to the matching ``settings`` fields. It returns ``None`` for an unknown
    name or a known provider that is not configured (missing client id or
    secret), so callers can treat both as "provider unavailable".

    Settings are imported lazily so that merely importing this package never
    forces configuration loading.
    """
    # Imported lazily (not at module top level) on purpose so that merely
    # importing this package never forces configuration loading.
    from engine.config import settings  # noqa: PLC0415

    normalized = name.lower()
    if normalized == "github":
        if not settings.github_client_id or not settings.github_client_secret:
            return None
        return GitHubOAuthProvider(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            redirect_uri=settings.github_redirect_uri,
        )
    if normalized == "google":
        if not settings.google_client_id or not settings.google_client_secret:
            return None
        return GoogleOAuthProvider(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.google_redirect_uri,
        )
    return None
