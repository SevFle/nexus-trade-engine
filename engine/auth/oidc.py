"""Generic OpenID Connect authentication provider.

This module implements a reusable, issuer-configurable OIDC provider for any
spec-compliant identity provider (Keycloak, Auth0, Okta, Dex, ...). It mirrors
the structure of the other providers in :mod:`engine.auth`
(:class:`~engine.auth.providers.google.GoogleOAuthProvider`,
:class:`~engine.auth.github.GitHubOAuthProvider`,
:class:`~engine.auth.providers.ldap.LDAPAuthProvider`) and exposes the same
:class:`~engine.auth.base.IOAuthProvider` contract, so it can be resolved
through :func:`engine.auth.get_oauth_provider` and plugged into the provider
registry exactly like the Google / GitHub providers.

The OIDC login flow is split into discrete, independently testable steps:

* :meth:`OIDCProvider.get_authorize_url` -- build the IdP authorization
  endpoint URL, embedding a caller-supplied CSRF ``state`` token, and return
  it together with the PKCE ``code_verifier`` (as an :class:`AuthURL`) that
  the caller must replay in :meth:`OIDCProvider.exchange_code`.
* :meth:`OIDCProvider.fetch_jwks` -- fetch the issuer's JSON Web Key Set over
  HTTPS via :mod:`httpx` (with caching), used to verify ID-token signatures.
* :meth:`OIDCProvider.verify_id_token` -- cryptographically verify an ID token
  against the JWKS keys, checking the ``iss`` / ``aud`` / ``exp`` (and
  ``nbf``/``sub``) claims and explicitly rejecting ``alg=none`` tokens.
* :meth:`OIDCProvider.exchange_code` -- trade an authorization code for a
  :class:`~engine.auth.base.TokenSet` (network + HTTP error handling).

Each failure mode raises a typed exception (all subclasses of
:class:`OIDCError`, and -- via the shared bases in :mod:`engine.auth.base` --
also catchable as :class:`~engine.auth.base.InvalidTokenError` /
:class:`~engine.auth.base.TokenExchangeError` /
:class:`~engine.auth.base.OAuthError`), so callers can either target the
OIDC-specific type or the shared base to handle the same failure family from
any provider.
"""

from __future__ import annotations

import base64
import hashlib
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

# HTTP status codes at or above this are error responses that must be surfaced
# as a typed provider error rather than parsed as a success body.
_HTTP_ERROR_STATUS_THRESHOLD = 400

# Default OIDC scopes. ``openid`` is mandatory for an OIDC flow; ``email`` and
# ``profile`` surface the claims we model in :class:`IDTokenClaims`.
_DEFAULT_SCOPE = "openid email profile"

# The claims that MUST be present on an OIDC ID token for it to be considered
# structurally valid (OIDC Core spec: iss, sub, aud, exp, iat). We require
# ``sub`` and omit a hard ``iat`` requirement because some IdPs (and unsigned
# test fixtures) omit it, but we still read it when present.
_REQUIRED_ID_TOKEN_CLAIMS = ("iss", "aud", "exp", "sub")

# Default allowed signing algorithms. Pinning the allowlist (rather than
# trusting whatever the JWT header claims) is what makes the ``alg=none``
# attack impossible -- PyJWT refuses any algorithm outside this set.
_DEFAULT_SIGNING_ALGS = ("RS256",)

# Hosts for which a non-HTTPS JWKS / endpoint URL is tolerated: local dev and
# the loopback address. Everything else must be HTTPS to protect the key
# material in transit.
_TLS_EXEMPT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# PKCE (RFC 7636) ``code_verifier`` generation. ``secrets.token_urlsafe(48)``
# yields a 64-character verifier composed of URL-safe characters, comfortably
# inside the spec's 43-128 character window while providing 384 bits of
# entropy -- more than enough to defeat brute-force/prediction of the
# challenge-binding secret.
_PKCE_VERIFIER_BYTES = 48

# The S256 ``code_challenge_method`` mandated by RFC 7636 (and the only one a
# modern, spec-conformant IdP should accept). ``plain`` is intentionally not
# supported because it offers no protection when the authorization request is
# intercepted (the whole point of PKCE for public/mobile clients).
_PKCE_CHALLENGE_METHOD = "S256"


def _derive_code_challenge(code_verifier: str) -> str:
    """Derive the S256 ``code_challenge`` from a ``code_verifier``.

    Per RFC 7636: ``BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`` with the
    trailing ``=`` padding stripped. The result is safe to embed directly in a
    query string.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a fresh PKCE ``(code_verifier, code_challenge)`` pair.

    ``code_verifier`` is a cryptographically strong random string
    (:func:`secrets.token_urlsafe`); ``code_challenge`` is its SHA-256 S256
    derivation (see :func:`_derive_code_challenge`). The verifier MUST be kept
    secret by the client and replayed in the token exchange, while the
    challenge is sent (in the clear) in the authorization request.

    A new pair should be generated for every authorization request so that a
    stolen challenge cannot be reused.
    """
    code_verifier = secrets.token_urlsafe(_PKCE_VERIFIER_BYTES)
    return code_verifier, _derive_code_challenge(code_verifier)


# --- Exceptions -------------------------------------------------------------
class OIDCError(OAuthError):
    """Base class for every error raised by the OIDC provider.

    Subclasses :class:`engine.auth.base.OAuthError` so a single
    ``except OAuthError`` catches OIDC-specific failures alongside the Google
    / GitHub / LDAP providers -- the package-wide contract.
    """


class InvalidTokenError(OIDCError, _InvalidTokenErrorBase):
    """Raised when an ID token fails verification (signature, issuer,
    audience, expiry, ``alg=none``, ...).

    Also subclasses the shared :class:`engine.auth.base.InvalidTokenError` so
    callers can catch the base to handle invalid tokens from *any* provider.
    Exported as :class:`OIDCInvalidTokenError` from :mod:`engine.auth` for
    callers that need the OIDC-specific type.
    """


# Provider-specific alias used by the package-level registry/exports so the
# OIDC variant stays distinguishable from the Google / GitHub ones.
OIDCInvalidTokenError = InvalidTokenError


class TokenExchangeError(OIDCError, _TokenExchangeErrorBase):
    """Raised when the authorization-code -> token exchange fails (network
    error or non-2xx response from the token endpoint).

    Also subclasses the shared :class:`engine.auth.base.TokenExchangeError`.
    """


# Provider-specific alias used by the package-level registry/exports.
OIDCTokenExchangeError = TokenExchangeError


class DiscoveryError(OIDCError):
    """Raised when JWKS / discovery fetching fails (network error, non-2xx
    HTTP, or a malformed JSON body).

    Distinct from :class:`TokenExchangeError` (token endpoint) so callers can
    tell "I could not fetch the keys" apart from "I could not exchange the
    code".
    """


# --- Data classes -----------------------------------------------------------
@dataclass
class IDTokenClaims:
    """Verified, type-safe view of the OIDC ID-token claims we rely on.

    ``raw`` preserves the full decoded payload for callers that need claims we
    do not model explicitly (e.g. group/role claims used for RBAC mapping).
    """

    iss: str
    aud: str
    sub: str
    exp: int
    iat: int
    email: str = ""
    email_verified: bool = False
    name: str | None = None
    preferred_username: str | None = None
    # The full decoded payload, for callers that need claims we don't model.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthURL:
    """Result of :meth:`OIDCProvider.get_authorize_url`.

    Carries the authorization-endpoint ``url`` to redirect the user to **and**
    the PKCE ``code_verifier`` that was bound to that URL's
    ``code_challenge``. The verifier MUST be replayed verbatim in the matching
    :meth:`OIDCProvider.exchange_code` call.

    Returning the verifier alongside the URL (instead of stashing it on the
    provider instance) keeps a single provider object stateless and therefore
    safe to share across concurrent authorization requests -- each caller owns
    its own verifier, so the authorize-step of one request can never leak or
    overwrite the verifier belonging to another in-flight request. The caller
    is responsible for persisting the verifier across the redirect (e.g. in a
    server-side session keyed by ``state``) and forwarding it to
    :meth:`OIDCProvider.exchange_code` on the callback.
    """

    url: str
    code_verifier: str


class _JWKSClient(Protocol):
    """Minimal slice of :class:`jwt.PyJWKClient` we depend on.

    Expressed as a Protocol so tests can inject a deterministic stub instead
    of relying on the httpx-fetched cache.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


def _enforce_https(url: str, *, what: str) -> None:
    """Reject non-HTTPS IdP URLs outside localhost.

    JWKS keys and OAuth2 endpoints carry sensitive material (signing keys,
    authorization codes, client secrets). Allowing ``http://`` for a remote
    host would expose them to a network observer, so we refuse it outright.
    Local dev (``localhost`` / loopback) is exempted for ergonomic testing.
    """
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return
    if scheme == "http" and host in _TLS_EXEMPT_HOSTS:
        return
    raise OIDCError(f"{what} must use HTTPS (http only allowed on {_TLS_EXEMPT_HOSTS}): {url!r}")


class OIDCProvider(IOAuthProvider):
    """Generic OpenID Connect provider.

    Parameters mirror a standard OIDC client registration. The ``issuer`` is
    used both as the ``iss`` claim value to enforce during ID-token
    verification *and* as the base for the default endpoints. Endpoint and
    JWKS URLs may be overridden explicitly (common when the IdP does not host
    discovery, or to point at a test fixture).

    For testability the HTTP ``transport`` and/or a ``jwks_client`` are
    injectable so unit tests can stub the JWKS endpoint and key resolution
    without touching the network.

    Parameters
    ----------
    issuer:
        The IdP issuer identifier (e.g. ``https://id.example.com/realms/main``).
        Used verbatim for the ID-token ``iss`` claim check.
    client_id / client_secret:
        The OIDC client registration credentials.
    redirect_uri:
        The OAuth2 callback URL registered with the IdP.
    jwks_uri:
        Override for the JWKS endpoint. Defaults to the issuer's well-known
        JWKS path; pass explicitly for non-conforming IdPs or for tests.
    authorize_endpoint / token_endpoint:
        Overrides for the authorization / token endpoints.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        jwks_uri: str | None = None,
        authorize_endpoint: str | None = None,
        token_endpoint: str | None = None,
        scope: str = _DEFAULT_SCOPE,
        signing_algs: tuple[str, ...] = _DEFAULT_SIGNING_ALGS,
        clock_skew: int = 0,
        jwks_client: _JWKSClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        http_timeout: float = 10.0,
    ) -> None:
        if not issuer or not isinstance(issuer, str):
            raise OIDCError("issuer is required")
        if not client_id:
            raise OIDCError("client_id is required")
        if not client_secret:
            raise OIDCError("client_secret is required")
        if not redirect_uri:
            raise OIDCError("redirect_uri is required")

        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scope = scope
        self._jwks_uri_override = jwks_uri
        self._authorize_endpoint_override = authorize_endpoint
        self._token_endpoint_override = token_endpoint
        self._signing_algs = tuple(signing_algs)
        self._clock_skew = clock_skew
        self._jwks_client = jwks_client
        self._transport = transport
        self._http_timeout = http_timeout
        # In-memory JWKS cache populated by :meth:`fetch_jwks`. Cleared by
        # passing ``force=True`` to :meth:`fetch_jwks`.
        self._jwks_cache: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """Stable lowercase provider identifier used as a registry key."""
        return "oidc"

    @property
    def jwks_uri(self) -> str:
        """Resolve the JWKS endpoint URL.

        An explicit override wins; otherwise we fall back to the issuer's
        well-known JWKS path. The default follows the common OIDC discovery
        convention but is intentionally overridable -- not every IdP serves
        JWKS at the same path, and tests point this at a mocked endpoint.
        """
        if self._jwks_uri_override:
            return self._jwks_uri_override
        return f"{self.issuer}/.well-known/jwks.json"

    @property
    def authorize_endpoint(self) -> str:
        """Resolve the authorization endpoint URL (explicit override or default)."""
        return self._authorize_endpoint_override or f"{self.issuer}/authorize"

    @property
    def token_endpoint(self) -> str:
        """Resolve the token endpoint URL (explicit override or default)."""
        return self._token_endpoint_override or f"{self.issuer}/token"

    # -- IOAuthProvider: authorization URL + CSRF state ---------------------
    def get_authorize_url(
        self,
        *,
        state: str,
        scope: str | None = None,
        code_verifier: str | None = None,
    ) -> AuthURL:
        """Build the IdP authorization endpoint URL (with PKCE S256).

        ``state`` is **required** -- it is the CSRF token. Building a URL
        without one would expose the callback to a login-CSRF attack, so we
        refuse to do it.

        PKCE (RFC 7636) is applied to every request: a fresh
        ``code_verifier`` is generated when one is not supplied, and its S256
        ``code_challenge`` is sent in the URL alongside
        ``code_challenge_method=S256``. The verifier is returned in the
        resulting :class:`AuthURL` -- it is NOT stored on the instance -- so
        the caller must carry it across the redirect and forward it to
        :meth:`exchange_code`. Callers that prefer their own verifier may pass
        it in via ``code_verifier`` (it is then echoed back unchanged in the
        :class:`AuthURL`).

        Returns an :class:`AuthURL` holding both the redirect ``url`` and the
        ``code_verifier`` that must be replayed at token-exchange time.
        """
        if not state:
            raise OIDCError("state is required for CSRF protection")

        if code_verifier is None:
            code_verifier, code_challenge = generate_pkce_pair()
        else:
            code_challenge = _derive_code_challenge(code_verifier)

        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": scope or self.scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": _PKCE_CHALLENGE_METHOD,
        }
        url = f"{self.authorize_endpoint}?{urllib.parse.urlencode(params)}"
        return AuthURL(url=url, code_verifier=code_verifier)

    @staticmethod
    def generate_pkce_pair() -> tuple[str, str]:
        """Convenience alias for :func:`generate_pkce_pair`.

        Exposed on the provider so callers can reach it through the same
        object they use for the rest of the OIDC flow.
        """
        return generate_pkce_pair()

    @staticmethod
    def generate_state() -> str:
        """Cryptographically strong opaque state token (delegates to base)."""
        return _generate_state()

    @staticmethod
    def validate_state(received_state: str | None, expected_state: str | None) -> None:
        """Confirm the echoed ``state`` matches the one we issued.

        Re-raises a base mismatch as the OIDC-typed error so callers only need
        to catch :class:`OIDCError` (or its base :class:`OAuthError`).
        Constant-time comparison happens in :func:`engine.auth.base.validate_state`.
        """
        try:
            _validate_state(received_state, expected_state)
        except OAuthError as exc:
            raise OIDCError(str(exc)) from exc

    # -- JWKS fetching -------------------------------------------------------
    async def fetch_jwks(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch (and cache) the issuer JWKS document via :mod:`httpx`.

        The result is cached for the lifetime of the provider; pass
        ``force=True`` to bypass the cache (e.g. after a key rotation has
        invalidated a cached ``kid``). Network and HTTP failures are wrapped
        in :class:`DiscoveryError`; a structurally invalid body (missing
        ``keys`` array) is also rejected.
        """
        if self._jwks_cache is not None and not force:
            return self._jwks_cache

        url = self.jwks_uri
        _enforce_https(url, what="JWKS endpoint")

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._http_timeout
            ) as client:
                response = await client.get(url)
        except httpx.RequestError as exc:
            logger.warning("auth.oidc.jwks.network_error", error=str(exc))
            raise DiscoveryError(f"network error fetching JWKS: {exc}") from exc

        if response.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
            logger.warning(
                "auth.oidc.jwks.http_error",
                status=response.status_code,
            )
            raise DiscoveryError(f"JWKS endpoint returned HTTP {response.status_code}")

        try:
            jwks = response.json()
        except ValueError as exc:
            raise DiscoveryError("JWKS endpoint returned a non-JSON body") from exc

        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise DiscoveryError("JWKS response is missing a 'keys' array")

        self._jwks_cache = jwks
        logger.debug("auth.oidc.jwks.fetched", key_count=len(jwks["keys"]))
        return jwks

    # -- ID-token verification ----------------------------------------------
    @staticmethod
    def _key_from_jwk(key_data: dict[str, Any], alg: str) -> Any:
        """Materialize a public key object from a JWK for the given algorithm.

        Supports the RSA (``RS*``) and Elliptic-Curve (``ES*``) families that
        IdPs overwhelmingly use for ID-token signing. An unsupported ``kty``
        is rejected loudly rather than silently accepted.
        """
        kty = key_data.get("kty")
        try:
            if kty == "RSA":
                return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
            if kty == "EC":
                return jwt.algorithms.ECAlgorithm.from_jwk(key_data)
        except (ValueError, TypeError) as exc:
            raise InvalidTokenError(f"invalid JWK for {alg!r}: {exc}") from exc
        raise InvalidTokenError(f"unsupported JWK key type: {kty!r}")

    def _resolve_signing_key(self, token: str, jwks: dict[str, Any]) -> Any:
        """Resolve the public key used to sign ``token`` from a JWKS document.

        Reads the (unverified) JWT header to pick ``alg`` and ``kid``; ``alg``
        is checked against the configured allowlist *before* key lookup so an
        ``alg=none`` token can never trick us into accepting a missing
        signature. The matching key is selected by ``kid``.
        """
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTInvalidTokenError as exc:
            raise InvalidTokenError(f"malformed id token header: {exc}") from exc

        alg = header.get("alg")
        if alg not in self._signing_algs:
            raise InvalidTokenError(
                f"unsupported signing algorithm: expected one of "
                f"{list(self._signing_algs)}, got {alg!r}"
            )

        kid = header.get("kid")
        for key_data in jwks.get("keys", []):
            if key_data.get("kid") == kid:
                return self._key_from_jwk(key_data, alg)
        raise InvalidTokenError(f"no JWKS key matched kid={kid!r}")

    def verify_id_token(
        self,
        token: str,
        *,
        audience: str | None = None,
        jwks: dict[str, Any] | None = None,
        jwks_client: _JWKSClient | None = None,
    ) -> IDTokenClaims:
        """Verify an OIDC ID token and return its validated claims.

        Key resolution order (first non-``None`` wins):

        1. ``jwks`` passed to this call,
        2. an explicit ``jwks_client`` (per-call override of the instance client),
        3. the instance ``jwks_client``,
        4. the cached JWKS populated by :meth:`fetch_jwks`.

        If none are available, :class:`InvalidTokenError` is raised prompting
        the caller to fetch JWKS first -- we never silently fetch over the
        network from a synchronous verification path.

        Verifies, in order: header ``alg`` allowlist (rejects ``alg=none``),
        signature, ``iss``, ``aud``, ``exp``/``nbf`` (with optional clock
        skew), and required-claim presence.

        Raises :class:`InvalidTokenError` on any failure.
        """
        if not token or not isinstance(token, str):
            raise InvalidTokenError("id token is required")

        # 1. Resolve the signing key.
        if jwks is not None:
            signing_key = self._resolve_signing_key(token, jwks)
        else:
            client = jwks_client or self._jwks_client
            if client is not None:
                try:
                    signing_key = client.get_signing_key_from_jwt(token).key
                except Exception as exc:
                    raise InvalidTokenError(f"unable to resolve signing key: {exc}") from exc
            elif self._jwks_cache is not None:
                signing_key = self._resolve_signing_key(token, self._jwks_cache)
            else:
                raise InvalidTokenError(
                    "no JWKS available: call fetch_jwks() first or pass jwks= / jwks_client="
                )

        expected_audience = audience or self.client_id
        payload = self._decode_payload(token, signing_key, expected_audience)

        logger.info(
            "auth.oidc.id_token_verified",
            sub=str(payload.get("sub", "")),
            iss=str(payload.get("iss", "")),
        )
        return IDTokenClaims(
            iss=str(payload.get("iss", "")),
            aud=str(payload.get("aud", "")),
            sub=str(payload.get("sub", "")),
            exp=int(payload.get("exp", 0)) if payload.get("exp") is not None else 0,
            iat=int(payload.get("iat", 0)) if payload.get("iat") is not None else 0,
            email=str(payload.get("email", "")),
            email_verified=bool(payload.get("email_verified", False)),
            name=payload.get("name"),
            preferred_username=payload.get("preferred_username"),
            raw=payload,
        )

    def _decode_payload(
        self,
        token: str,
        signing_key: Any,
        expected_audience: str,
    ) -> dict[str, Any]:
        """Decode + verify the ID token, mapping PyJWT errors to ours.

        ``algorithms`` is pinned to the configured allowlist so even a key/alg
        confusion cannot succeed, and ``require`` enforces the structural OIDC
        claims (iss/aud/exp/sub). Each PyJWT failure is translated into a
        descriptive :class:`InvalidTokenError` so callers see a uniform type.
        """
        try:
            return jwt.decode(
                token,
                key=signing_key,
                algorithms=list(self._signing_algs),
                audience=expected_audience,
                issuer=self.issuer,
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

    # -- Authorization-code exchange ----------------------------------------
    async def exchange_code(self, code: str, *, code_verifier: str) -> TokenSet:
        """Exchange an authorization code for a :class:`TokenSet`.

        Any transport-level failure or non-2xx HTTP response from the token
        endpoint is wrapped in :class:`TokenExchangeError` so callers see a
        single typed failure mode.

        ``code_verifier`` is **required**: it is the PKCE verifier bound to the
        ``code_challenge`` that :meth:`get_authorize_url` embedded in the
        authorization URL, and the IdP rejects the exchange unless the two
        match. Because the provider no longer remembers the verifier between
        calls (that instance state was unsafe under concurrent use), the
        caller MUST thread the :class:`AuthURL.code_verifier` it received from
        :meth:`get_authorize_url` back in here -- typically by persisting it
        in a server-side session keyed by the ``state`` token across the
        authorization redirect.
        """
        if not code:
            raise TokenExchangeError("authorization code is required")
        if not code_verifier:
            raise TokenExchangeError("code_verifier is required")

        token_url = self.token_endpoint
        _enforce_https(token_url, what="token endpoint")

        data: dict[str, str] = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._http_timeout
            ) as client:
                response = await client.post(token_url, data=data)
        except httpx.RequestError as exc:
            logger.warning("auth.oidc.token_exchange.network_error", error=str(exc))
            raise TokenExchangeError(f"network error contacting token endpoint: {exc}") from exc

        if response.status_code >= _HTTP_ERROR_STATUS_THRESHOLD:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error") or body)
            except (ValueError, TypeError):
                detail = response.text
            logger.warning(
                "auth.oidc.token_exchange.http_error",
                status=response.status_code,
                detail=detail[:200],
            )
            raise TokenExchangeError(
                f"token endpoint returned HTTP {response.status_code}: {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenExchangeError("token endpoint returned a non-JSON body") from exc

        if not payload or "access_token" not in payload:
            raise TokenExchangeError("token endpoint response missing access_token")

        logger.info("auth.oidc.token_exchange.success")
        return TokenSet(
            access_token=payload["access_token"],
            token_type=payload.get("token_type", "Bearer"),
            expires_in=payload.get("expires_in"),
            refresh_token=payload.get("refresh_token"),
            scope=payload.get("scope"),
            raw=payload,
        )
