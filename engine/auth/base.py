"""Shared OAuth2 provider interface, exception hierarchy, and helpers.

This module defines the provider-agnostic building blocks that every OAuth2 /
OIDC identity provider in :mod:`engine.auth` reuses:

* :class:`OAuthError` -- the common base of **every** error raised by an
  OAuth2 provider in this package. Catch it to handle any provider failure.
* :class:`InvalidTokenError` / :class:`TokenExchangeError` -- the *shared*
  bases for the two failure families providers share (token verification and
  authorization-code exchange). Concrete providers subclass these so a caller
  can catch, e.g., "any invalid token" with a single ``except`` without
  enumerating every provider's variant. See the provider modules for the
  provider-specific subclasses
  (:class:`engine.auth.providers.google.InvalidTokenError`,
  :class:`engine.auth.github.InvalidTokenError`, ...).
* :func:`generate_state` / :func:`validate_state` -- CSRF ``state`` token
  generation and constant-time comparison (an OAuth2 login-CSRF defense).
* :class:`TokenSet` -- the normalized shape of a successful
  authorization-code -> token exchange.
* :class:`IOAuthProvider` -- the common contract every concrete provider
  satisfies.

Token *validation* intentionally lives outside this interface: Google verifies
a signed OIDC ID token, whereas GitHub introspects an opaque access token
against its ``/user`` API. Each provider therefore exposes its own validation
method while still sharing the authorization-URL / CSRF surface defined here.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --- Shared exception hierarchy --------------------------------------------
class OAuthError(Exception):
    """Base class for every error raised by an OAuth2/OIDC provider.

    All provider-specific error classes (and the shared
    :class:`InvalidTokenError` / :class:`TokenExchangeError` bases below) are
    subclasses of this, so ``except OAuthError`` is the catch-all for "the
    provider rejected the request".
    """


class InvalidTokenError(OAuthError):
    """Shared base for token-verification failures across all providers.

    Each provider defines its own subclass (e.g.
    :class:`engine.auth.providers.google.InvalidTokenError`,
    :class:`engine.auth.github.InvalidTokenError`) that ultimately derives
    from this base. Catching :class:`InvalidTokenError` therefore catches
    *every* provider's invalid-token variant, which is exactly what a caller
    that only cares "the token was bad" wants.
    """


class TokenExchangeError(OAuthError):
    """Shared base for authorization-code -> token exchange failures.

    Concrete providers subclass this for their transport-level and HTTP-error
    failure modes during the code-for-token swap; catching the base catches
    them all uniformly.
    """


# --- Provider-agnostic helpers ---------------------------------------------
def generate_state() -> str:
    """Return a cryptographically strong, opaque CSRF state token.

    Used as the ``state`` parameter in an OAuth2 authorization request and
    later compared (via :func:`validate_state`) against the value echoed back
    by the identity provider on the callback.
    """
    return secrets.token_urlsafe(32)


def validate_state(received_state: str | None, expected_state: str | None) -> None:
    """Constant-time comparison of the echoed ``state`` against the one issued.

    A mismatch (or a missing/empty value on either side) raises
    :class:`OAuthError`; callers MUST treat that as a rejected request to
    defend against login-CSRF. :func:`hmac.compare_digest` is used to avoid a
    timing oracle.
    """
    if not isinstance(received_state, str) or not isinstance(expected_state, str):
        raise OAuthError("CSRF state validation failed: missing state")
    if not received_state or not expected_state:
        raise OAuthError("CSRF state validation failed: empty state")
    if not hmac.compare_digest(received_state, expected_state):
        raise OAuthError("CSRF state mismatch")


@dataclass
class TokenSet:
    """Normalized result of a successful authorization-code exchange.

    Concrete providers populate this from their token endpoint's JSON response;
    the raw payload is preserved for callers that need provider-specific fields
    (Google's OIDC ``id_token`` is modelled by Google's own subclass).
    """

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token-type literal, not a secret
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class IOAuthProvider(Protocol):
    """Common OAuth2 provider contract.

    Every provider in :mod:`engine.auth` exposes:

    * :attr:`name` -- a stable lowercase identifier (``"github"``, ``"google"``)
      used as the registry key.
    * :meth:`get_authorize_url` -- build the IdP authorization endpoint URL,
      embedding a caller-supplied CSRF ``state`` token.
    * :meth:`generate_state` / :meth:`validate_state` -- CSRF ``state`` handling.

    Authorization-code exchange and token validation are provider-specific and
    therefore not part of this interface.
    """

    @property
    def name(self) -> str: ...

    def get_authorize_url(self, *, state: str, scope: str = "openid email profile") -> str: ...

    @staticmethod
    def generate_state() -> str: ...

    @staticmethod
    def validate_state(received_state: str | None, expected_state: str | None) -> None: ...
