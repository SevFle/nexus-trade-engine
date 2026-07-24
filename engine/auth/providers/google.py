"""Google OAuth2 / OIDC authentication provider.

This module splits the Google sign-in flow into discrete, independently
testable steps:

* :meth:`GoogleOAuthProvider.get_authorize_url` -- build the authorization
  endpoint URL (with a CSRF ``state`` parameter).
* :meth:`GoogleOAuthProvider.validate_state` -- confirm the ``state`` echoed
  back by Google matches the one we issued (CSRF defense).
* :meth:`GoogleOAuthProvider.exchange_code` -- trade an authorization code for
  a token set over HTTPS (network + status error handling).
* :meth:`GoogleOAuthProvider.exchange_code_for_token` -- self-documenting alias
  for :meth:`exchange_code` named after the canonical OAuth2 "code-for-token"
  step (forwards an optional PKCE ``code_verifier``).
* :meth:`GoogleOAuthProvider.get_user_info` -- resolve an access token to a
  normalized :class:`GoogleUserInfo` via the ``oauth2/v2/userinfo`` endpoint
  (the OAuth2 access-token counterpart to the OIDC ID-token path).
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

from engine.auth.base import (
    InvalidTokenError as _InvalidTokenErrorBase,
)
from engine.auth.base import (
    OAuthError,
)
from engine.auth.base import (
    TokenExchangeError as _TokenExchangeErrorBase,
)
from engine.auth.base import (
    TokenSet as BaseTokenSet,
)

logger = structlog.get_logger()

# --- Public endpoints -------------------------------------------------------
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint URL, not a secret
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
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
class GoogleOAuthError(OAuthError):
    """Base class for every error raised by the Google OAuth2 provider.

    Subclasses :class:`engine.auth.base.OAuthError` so a single
    ``except OAuthError`` catches Google-specific failures too.
    """


class InvalidTokenError(GoogleOAuthError, _InvalidTokenErrorBase):
    """Raised when an ID token fails verification (signature, issuer,
    audience, expiry, ``alg=none``, ...).

    Also subclasses the shared :class:`engine.auth.base.InvalidTokenError` so
    callers can catch the base to handle invalid tokens from *any* provider.
    Exported as :class:`GoogleInvalidTokenError` from
    :mod:`engine.auth` for callers that need the Google-specific type.
    """


# Provider-specific alias used by the package-level registry/exports so the
# Google variant stays distinguishable from the GitHub one.
GoogleInvalidTokenError = InvalidTokenError


class TokenExchangeError(GoogleOAuthError, _TokenExchangeErrorBase):
    """Raised when the authorization-code -> token exchange fails (network
    error or non-2xx response from the token endpoint).

    Also subclasses the shared :class:`engine.auth.base.TokenExchangeError` so
    callers can catch the base to handle exchange failures from *any*
    provider.
    """


# Provider-specific alias used by the package-level registry/exports.
GoogleTokenExchangeError = TokenExchangeError


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
class GoogleUserInfo:
    """Normalized, type-safe view of a validated Google user profile.

    Populated by :meth:`GoogleOAuthProvider.get_user_info` from the
    ``oauth2/v2/userinfo`` endpoint. Google returns ``sub`` as the stable,
    globally-unique account identifier; this is exposed as ``provider_id`` --
    the field the rest of the engine links an OAuth-backed user on (analogous
    to GitHub's numeric account ``id``). ``email`` is present only when the
    ``email``/``openid`` scopes were granted, and ``name`` is the display name.
    """

    provider_id: str
    email: str = ""
    name: str = ""
    avatar_url: str | None = None
    email_verified: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenSet(BaseTokenSet):
    """Normalized result of a successful token exchange (Google-specific).

    Subclasses the shared :class:`engine.auth.base.TokenSet` so a caller can
    treat every provider's token set uniformly (an ``isinstance`` check
    against the base works) while still carrying Google's OIDC ``id_token``.
    """

    id_token: str | None = None


def _coerce_email_verified(value: Any) -> bool:
    """Coerce Google's ``email_verified`` claim to a real bool.

    Google documents ``email_verified`` as a boolean, but some flows/clients
    serialize it as the string ``"true"``/``"false"``. Relying on ``bool()``
    alone is unsafe because ``bool("false")`` is ``True`` (any non-empty
    string is truthy), so an *unverified* email would masquerade as verified.
    We therefore normalize explicitly before exposing the value downstream.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


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

    async def exchange_code_for_token(
        self, code: str, *, code_verifier: str | None = None
    ) -> TokenSet:
        """Exchange an authorization code for a :class:`TokenSet`.

        Self-documenting alias for :meth:`exchange_code` named after the
        canonical "code-for-token" step of the OAuth2 authorization-code flow.
        It forwards the optional PKCE ``code_verifier`` so a caller that opts
        into PKCE keeps that defense end-to-end. Kept as a thin delegate
        (rather than a rename) so existing callers of :meth:`exchange_code`
        continue to work unchanged.
        """
        return await self.exchange_code(code, code_verifier=code_verifier)

    # -- User profile via the userinfo endpoint ----------------------------
    async def get_user_info(self, access_token: str) -> GoogleUserInfo:
        """Resolve an access token to a normalized :class:`GoogleUserInfo`.

        The opaque ``access_token`` returned by :meth:`exchange_code` (or
        :meth:`exchange_code_for_token`) is presented as a Bearer credential to
        ``https://www.googleapis.com/oauth2/v2/userinfo`` and the JSON response
        is normalized into a :class:`GoogleUserInfo`. The stable account
        identifier is Google's ``sub`` claim, exposed as ``provider_id``.

        This is the OAuth2 (access-token) counterpart to the OIDC
        :meth:`verify_id_token` path: use it when a code was exchanged for an
        access token rather than validating a signed ID token.

        Raises :class:`InvalidTokenError` for a missing/empty token, an HTTP
        401, any other HTTP error, a non-JSON body, a transport-level failure,
        or a profile missing the required ``sub`` identifier -- mirroring the
        failure contract of
        :meth:`engine.auth.github.GitHubOAuthProvider.validate_access_token`.
        """
        if not access_token or not isinstance(access_token, str):
            raise InvalidTokenError("access token is required")

        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        profile: dict[str, Any]
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=httpx.Timeout(10.0)
            ) as client:
                resp = await client.get(_GOOGLE_USERINFO_URL, headers=headers)
        except httpx.RequestError as exc:
            logger.warning("auth.google.userinfo.network_error", error=str(exc))
            raise InvalidTokenError(
                f"network error contacting userinfo endpoint: {exc}"
            ) from exc

        if resp.status_code == httpx.codes.UNAUTHORIZED:
            raise InvalidTokenError("access token is invalid or expired")
        if resp.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
            raise InvalidTokenError(
                f"userinfo endpoint returned HTTP {resp.status_code}"
            )

        try:
            profile = resp.json()
        except ValueError as exc:
            raise InvalidTokenError("userinfo endpoint returned non-JSON body") from exc

        sub = profile.get("sub") if isinstance(profile, dict) else None
        if not sub:
            raise InvalidTokenError("incomplete Google profile (missing sub)")

        email = profile.get("email") or ""
        name = profile.get("name")
        if not name:
            # Fall back to the email local-part when Google omits the display
            # name (mirrors GitHub's login fallback for a null ``name``).
            name = email.split("@")[0] if email else ""

        logger.info("auth.google.userinfo.success")
        return GoogleUserInfo(
            provider_id=str(sub),
            email=str(email),
            name=str(name),
            avatar_url=profile.get("picture"),
            email_verified=_coerce_email_verified(profile.get("email_verified", False)),
            raw=profile,
        )

    def map_user(self, info: GoogleUserInfo) -> dict[str, Any]:
        """Map a validated Google profile to the Nexus user-model shape.

        Symmetric with
        :meth:`engine.auth.github.GitHubOAuthProvider.map_user`: ``external_id``
        is the stable Google ``sub`` (``provider_id``), and ``display_name``
        falls back to the email local-part (then a generic label) when no
        display name was provided.
        """
        display_name = info.name
        if not display_name:
            display_name = info.email.split("@")[0] if info.email else "Google User"
        return {
            "external_id": info.provider_id,
            "provider": self.name,
            "email": info.email,
            "display_name": display_name,
            "email_verified": info.email_verified,
            "roles": ["user"],
        }

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
            email_verified=_coerce_email_verified(payload.get("email_verified", False)),
            name=payload.get("name"),
            picture=payload.get("picture"),
            locale=payload.get("locale"),
            raw=payload,
        )
