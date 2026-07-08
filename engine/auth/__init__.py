"""Authentication providers package.

This package hosts well-decomposed, protocol-aware identity providers
(OAuth2 / OIDC). It complements :mod:`engine.api.auth` (which holds the
FastAPI integration, base classes, and the provider registry) with providers
that split each OAuth2 flow into its discrete steps so they can be tested
independently.
"""

from __future__ import annotations

from engine.auth.providers.google import (
    GoogleOAuthError,
    GoogleOAuthProvider,
    IDTokenClaims,
    InvalidTokenError,
    TokenExchangeError,
    TokenSet,
)

__all__ = [
    "GoogleOAuthError",
    "GoogleOAuthProvider",
    "IDTokenClaims",
    "InvalidTokenError",
    "TokenExchangeError",
    "TokenSet",
]
