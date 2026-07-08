"""Shared OAuth2 provider interface and helpers.

This module defines :class:`IOAuthProvider` -- the common contract that every
OAuth2/OIDC identity provider in :mod:`engine.auth` satisfies -- together with
provider-agnostic helpers that concrete providers reuse:

* :func:`generate_state` / :func:`validate_state` -- CSRF ``state`` token
  generation and constant-time comparison (an OAuth2 login-CSRF defense).
* :class:`TokenSet` -- the normalized shape of a successful
  authorization-code -> token exchange.

Token *validation* intentionally lives outside this interface: Google verifies
a signed OIDC ID token, whereas GitHub introspects an opaque access token
against its ``/user`` API. Each provider therefore exposes its own
validation method (:meth:`~engine.auth.github.GitHubOAuthProvider.validate_access_token`
for GitHub, :meth:`~engine.auth.providers.google.GoogleOAuthProvider.verify_id_token`
for Google) while still sharing the authorization-URL / CSRF surface defined
here.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class OAuthError(Exception):
    """Base class for errors raised by OAuth2 providers in this package."""


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
    the raw payload is preserved for callers that need provider-specific fields.
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
