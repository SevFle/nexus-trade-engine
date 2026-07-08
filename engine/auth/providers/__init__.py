"""Concrete authentication provider implementations.

Re-exports the shared base types from :mod:`engine.auth.base` alongside the
provider-specific classes. Provider exception subclasses are exported under
clear, prefixed aliases (``GoogleInvalidTokenError``,
``GitHubInvalidTokenError``, ...) so callers can target a single provider,
while the un-prefixed ``InvalidTokenError`` / ``TokenExchangeError`` /
``OAuthError`` names resolve to the shared *bases* -- catching them handles
every provider's variant.
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
    "TokenExchangeError",
    "TokenSet",
    "generate_state",
    "validate_state",
]
