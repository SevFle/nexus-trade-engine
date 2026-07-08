"""Concrete authentication provider implementations."""

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
    "validate_state",
]
