"""Google OAuth2 / OIDC authentication provider.

This module splits the Google sign-in flow into discrete, independently
testable steps:

* :meth:`GoogleOAuthProvider.get_authorize_url` -- build the authorization
  endpoint URL (with a CSRF ``state`` parameter).
* :meth:`GoogleOAuthProvider.validate_state` -- confirm the ``state`` echoed
  back by Google matches the one we issued (CSRF defense).
* :meth:`GoogleOAuthProvider.exchange_code` -- trade an authorization code for
  a token set over HTTPS (network + status error handling).
* :meth:`GoogleOAuthProvider.verify_id_token` -- cryptographically verify the
  ID token returned by Google (signature, issuer, audience, expiry and
  ``alg=none`` rejection).

Each step raises a typed exception (all subclasses of
:class:`GoogleOAuthError`) so callers can distinguish failure modes.
"""

from __future__ import annotations

import hmac
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import jwt
import structlog
from jwt.exceptions import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAlgorithmError,
    InvalidAudienceError,
    InvalidIssuerError,
)
from jwt.exceptions import (
    InvalidTokenError as PyJWTInvalidTokenError,
)

logger = structlog.get_logger()

# --- Public endpoints -------------------------------------------------------
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint URL, not a secret
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Google's documented ID-token issuers. An ID token is only valid if its
# ``iss`` claim is one of these (see Google identity docs).
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# HTTP status codes at or above this are error responses from the token
# endpoint that must be surfaced as a :class:`TokenExchangeError`.
_HTTP_ERROR_STATUS_THRESHOLD = 400

# The only signing algorithm Google ever uses for ID tokens. Pinning it
# (rather than accepting whatever the JWT header claims) is what makes the
# ``alg=none`` attack impossible -- PyJWT refuses any algorithm outside this
# allowlist.
_ALLOWED_SIGNING_ALG = "RS256"

# Claims that MUST be present on a Google ID token for it to be considered
# structurally valid.
_REQUIRED_ID_TOKEN_CLAIMS = ("iss", "aud", "exp", "sub")


# --- Exceptions -------------------------------------------------------------
class GoogleOAuthError(Exception):
    """Base class for every error raised by the Google OAuth2 provider."""


class InvalidTokenError(GoogleOAuthError):
    """Raised when an ID token fails verification (signature, issuer,
    audience, expiry, ``alg=none``, ...)."""


class TokenExchangeError(GoogleOAuthError):
    """Raised when the authorization-code -> token exchange fails (network
    error or non-2xx response from the token endpoint)."""


# --- Data classes -----------------------------------------------------------
@dataclass
class IDTokenClaims:
    """Verified, type-safe view of the Google ID-token claims we rely on."""

    iss: str
    aud: str
    sub: str
    exp: int
    iat: int
    email: str = ""
    email_verified: bool = False
    name: str | None = None
    picture: str | None = None
    locale: str | None = None
    # The full decoded payload, for callers that need claims we don't model.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenSet:
    """Normalized result of a successful token exchange."""

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token-type literal, not a secret
    expires_in: int | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    scope: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class _JWKSClient(Protocol):
    """Minimal slice of :class:`jwt.PyJWKClient` we depend on.

    Expressed as a Protocol so tests can inject a deterministic stub without
    touching the network.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class _PyJWKClientShim:
    """Thin lazy wrapper around :class:`jwt.PyJWKClient`.

    Created lazily so merely importing the provider (or unit-testing
    :meth:`verify_id_token` with an injected client) never performs I/O.
    """

    def __init__(self, jwks_url: str) -> None:
        self._jwks_url = jwks_url
        self._client: Any = None

    def get_signing_key_from_jwt(self, token: str) -> Any:
        if self._client is None:  # pragma: no cover - exercised indirectly
            self._client = jwt.PyJWKClient(self._jwks_url)
        return self._client.get_signing_key_from_jwt(token)


class GoogleOAuthProvider:
    """Google OAuth2 / OIDC provider.

    Parameters mirror the standard Google OAuth2 client configuration. For
    testability the JWKS source (``jwks_client``) and the HTTP transport
    (``transport``) are injectable so no network access is required in unit
    tests.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        jwks_client: _JWKSClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        issuer: str | tuple[str, ...] = _GOOGLE_ISSUERS,
        clock_skew: int = 0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._jwks_client = jwks_client
        self._transport = transport
        self._issuer = issuer
        self._clock_skew = clock_skew

    @property
    def name(self) -> str:
        return "google"

    # -- Authorization URL ---------------------------------------------------
    def get_authorize_url(
        self,
        *,
        state: str,
        scope: str = "openid email profile",
        access_type: str = "online",
        prompt: str = "select_account",
    ) -> str:
        """Build the Google authorization endpoint URL.

        ``state`` is **required** -- it is the CSRF token. Building a URL
        without one would expose the callback to a login-CSRF attack, so we
        refuse to do it.
        """
        if not state:
            raise GoogleOAuthError("state is required for CSRF protection")
        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "access_type": access_type,
            "prompt": prompt,
            "include_granted_scopes": "true",
        }
        return f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def generate_state() -> str:
        """Cryptographically strong opaque state token."""
        return secrets.token_urlsafe(32)

    # -- CSRF state validation ----------------------------------------------
    @staticmethod
    def validate_state(received_state: str | None, expected_state: str | None) -> None:
        """Confirm the ``state`` echoed back matches the one we issued.

        Uses :func:`hmac.compare_digest` for constant-time comparison to
        avoid timing oracles. Raises :class:`GoogleOAuthError` on any
        mismatch (including missing values), which callers MUST treat as a
        rejected request.
        """
        if not isinstance(received_state, str) or not isinstance(expected_state, str):
            raise GoogleOAuthError("CSRF state validation failed: missing state")
        if not received_state or not expected_state:
            raise GoogleOAuthError("CSRF state validation failed: empty state")
        if not hmac.compare_digest(received_state, expected_state):
            raise GoogleOAuthError("CSRF state mismatch")

    # -- Authorization-code exchange ----------------------------------------
    async def exchange_code(self, code: str, *, code_verifier: str | None = None) -> TokenSet:
        """Exchange an authorization code for a :class:`TokenSet`.

        Any transport-level failure (DNS, connection refused, TLS, timeout)
        or non-2xx HTTP response is wrapped in :class:`TokenExchangeError`
        so callers see a single, typed failure mode.
        """
        if not code:
            raise TokenExchangeError("authorization code is required")

        data: dict[str, str] = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier

        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(_GOOGLE_TOKEN_URL, data=data)
        except httpx.RequestError as exc:
            logger.warning("auth.google.token_exchange.network_error", error=str(exc))
            raise TokenExchangeError(f"network error contacting token endpoint: {exc}") from exc

        if response.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
            # Try to surface Google's structured error body for diagnostics.
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error") or body)
            except (ValueError, TypeError):
                detail = response.text
            logger.warning(
                "auth.google.token_exchange.http_error",
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

        logger.info("auth.google.token_exchange.success")
        return TokenSet(
            access_token=payload["access_token"],
            token_type=payload.get("token_type", "Bearer"),
            expires_in=payload.get("expires_in"),
            refresh_token=payload.get("refresh_token"),
            id_token=payload.get("id_token"),
            scope=payload.get("scope"),
            raw=payload,
        )

    # -- ID-token verification ----------------------------------------------
    def verify_id_token(
        self,
        token: str,
        *,
        audience: str | None = None,
        jwks_client: _JWKSClient | None = None,
    ) -> IDTokenClaims:
        """Verify a Google ID token and return its validated claims.

        Verifies, in order:

        1. The header ``alg`` is the pinned ``RS256`` -- this explicitly
           rejects ``alg=none`` (unsigned) tokens *before* any key lookup.
        2. The signature is valid against a Google signing key.
        3. ``iss`` is one of Google's issuers.
        4. ``aud`` is our client id (or ``audience`` if overridden).
        5. ``exp``/``nbf`` are honored (with optional clock skew leeway).
        6. All required claims are present.

        Any failure raises :class:`InvalidTokenError`.
        """
        if not token or not isinstance(token, str):
            raise InvalidTokenError("id token is required")

        # 1. Header / algorithm check. Done *before* key lookup so an
        #    ``alg=none`` token can never trick us into accepting a missing
        #    signature.
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTInvalidTokenError as exc:
            raise InvalidTokenError(f"malformed id token header: {exc}") from exc

        alg = header.get("alg")
        if alg != _ALLOWED_SIGNING_ALG:
            raise InvalidTokenError(
                f"unsupported signing algorithm: expected {_ALLOWED_SIGNING_ALG!r}, got {alg!r}"
            )

        # 2. Resolve the signing key. Prefer the per-call override, then the
        #    instance client, then a lazily-created PyJWKClient.
        jwks = jwks_client or self._jwks_client
        if jwks is None:
            jwks = _PyJWKClientShim(_GOOGLE_JWKS_URL)
        try:
            signing_key = jwks.get_signing_key_from_jwt(token).key
        except Exception as exc:
            raise InvalidTokenError(f"unable to resolve signing key: {exc}") from exc

        expected_audience = audience or self.client_id

        # 3-6. Full verification. ``algorithms`` is pinned to RS256 so even a
        #      key/alg confusion cannot succeed; ``require`` enforces claim
        #      presence.
        try:
            payload = jwt.decode(
                token,
                key=signing_key,
                algorithms=[_ALLOWED_SIGNING_ALG],
                audience=expected_audience,
                issuer=self._issuer,
                leeway=self._clock_skew,
                options={"require": list(_REQUIRED_ID_TOKEN_CLAIMS)},
            )
        except ExpiredSignatureError as exc:
            raise InvalidTokenError("id token is expired") from exc
        except ImmatureSignatureError as exc:
            raise InvalidTokenError("id token not yet valid") from exc
        except InvalidIssuerError as exc:
            raise InvalidTokenError("id token has wrong issuer") from exc
        except InvalidAudienceError as exc:
            raise InvalidTokenError("id token has wrong audience") from exc
        except InvalidAlgorithmError as exc:
            raise InvalidTokenError(f"invalid signing algorithm: {exc}") from exc
        except PyJWTInvalidTokenError as exc:
            raise InvalidTokenError(f"id token verification failed: {exc}") from exc

        return IDTokenClaims(
            iss=str(payload.get("iss", "")),
            aud=str(payload.get("aud", "")),
            sub=str(payload.get("sub", "")),
            exp=int(payload.get("exp", 0)) if payload.get("exp") is not None else 0,
            iat=int(payload.get("iat", 0)) if payload.get("iat") is not None else 0,
            email=str(payload.get("email", "")),
            email_verified=bool(payload.get("email_verified", False)),
            name=payload.get("name"),
            picture=payload.get("picture"),
            locale=payload.get("locale"),
            raw=payload,
        )
