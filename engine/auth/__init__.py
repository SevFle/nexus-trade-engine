"""Authentication providers package.

This package hosts well-decomposed, protocol-aware identity providers
(OAuth2 / OIDC). It complements :mod:`engine.api.auth` (which holds the
FastAPI integration, base classes, and the provider registry) with providers
that split each OAuth2 flow into its discrete steps so they can be tested
independently.

All concrete providers satisfy the :class:`IOAuthProvider` interface declared
in :mod:`engine.auth.base`. Use :func:`get_oauth_provider` to resolve a
provider by name from the application configuration.

Shared exception bases
----------------------
The un-prefixed :class:`InvalidTokenError`, :class:`TokenExchangeError` and
:class:`OAuthError` names exported here resolve to the **shared bases** in
:mod:`engine.auth.base`. Provider-specific subclasses are exported under
clear, prefixed aliases so a caller can either catch the shared base (to
handle the failure family from *any* provider) or a single provider's
variant:

* ``except InvalidTokenError`` -- catches ``GoogleInvalidTokenError`` *and*
  ``GitHubInvalidTokenError``.
* ``except TokenExchangeError`` -- catches ``GoogleTokenExchangeError`` *and*
  ``GitHubTokenExchangeError``.
"""

from __future__ import annotations

from engine.auth.base import (
    InvalidTokenError,
    IOAuthProvider,
    OAuthError,
    TokenExchangeError,
    TokenSet,
    generate_state,
    validate_state,
)
from engine.auth.github import (
    GitHubOAuthError,
    GitHubOAuthProvider,
    GitHubUserInfo,
)
from engine.auth.github import (
    InvalidTokenError as GitHubInvalidTokenError,
)
from engine.auth.github import (
    TokenExchangeError as GitHubTokenExchangeError,
)
from engine.auth.oidc import (
    DiscoveryError,
    OIDCError,
    OIDCProvider,
)
from engine.auth.oidc import (
    IDTokenClaims as OIDCIDTokenClaims,
)
from engine.auth.oidc import (
    InvalidTokenError as OIDCInvalidTokenError,
)
from engine.auth.oidc import (
    TokenExchangeError as OIDCTokenExchangeError,
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
    "DiscoveryError",
    "GitHubInvalidTokenError",
    "GitHubOAuthError",
    "GitHubOAuthProvider",
    "GitHubTokenExchangeError",
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
    "OIDCError",
    "OIDCIDTokenClaims",
    "OIDCInvalidTokenError",
    "OIDCProvider",
    "OIDCTokenExchangeError",
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
    to the matching ``settings`` fields.

    Return policy:

    * A known provider that is **not configured** (missing client id or
      secret) returns ``None`` so callers can treat it as "provider
      unavailable".
    * An **unknown** provider name raises :class:`ValueError` -- this is a
      programmer/caller error (typo, unsupported IdP) rather than a missing
      configuration, so it is surfaced loudly instead of being silently
      swallowed.

    Settings are imported lazily so that merely importing this package never
    forces configuration loading.
    """
    # Imported lazily (not at module top level) on purpose so that merely
    # importing this package never forces configuration loading.
    from engine.config import settings  # noqa: PLC0415

    normalized = (name or "").lower()
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
    if normalized == "oidc":
        # The generic OIDC provider needs an issuer and a registered client.
        # ``oidc_jwks_uri`` is optional (defaults to the issuer's well-known
        # path). Returns ``None`` when unconfigured, matching the policy for
        # the other providers so callers treat it as "provider unavailable".
        if not settings.oidc_issuer or not settings.oidc_client_id:
            return None
        return OIDCProvider(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
            jwks_uri=settings.oidc_jwks_uri or None,
        )
    raise ValueError(f"Unknown OAuth provider: {name!r}")
