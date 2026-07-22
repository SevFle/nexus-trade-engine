"""Unit tests for the generic OIDC provider (engine.auth.oidc).

The headline test exercises the full happy path end-to-end against a *mocked*
JWKS endpoint:

1. An RSA signing keypair is generated; the public key is published as a JWK
   with a ``kid``.
2. A JWKS document containing that key is served by an ``httpx.MockTransport``
   (no real network).
3. ``OIDCProvider.fetch_jwks()`` retrieves it via httpx and caches it.
4. ``OIDCProvider.verify_id_token()`` validates an ID token signed with the
   matching private key, asserting the ``iss`` / ``aud`` / ``exp`` claims.

A handful of focused negative tests then pin the security-relevant checks the
headline test does not exercise directly: issuer / audience / expiry
enforcement, ``alg=none`` rejection, unknown ``kid``, and the HTTPS-only guard
on the JWKS endpoint.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import MockTransport

from engine.auth import get_oauth_provider
from engine.auth.base import InvalidTokenError as BaseInvalidTokenError
from engine.auth.base import IOAuthProvider
from engine.auth.oidc import (
    DiscoveryError,
    IDTokenClaims,
    OIDCError,
    OIDCInvalidTokenError,
    OIDCProvider,
    OIDCTokenExchangeError,
)

ISSUER = "https://id.example.com"
CLIENT_ID = "nexus-client"
CLIENT_SECRET = "super-secret"
REDIRECT_URI = "https://app.example.com/callback"


# --- Key / token helpers ---------------------------------------------------
def _generate_rsa_keypair() -> tuple[Any, Any]:
    """Generate an RSA private/public keypair for signing test ID tokens."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_jwk(public_key: Any, kid: str = "test-key-1") -> tuple[dict[str, Any], str]:
    """Serialize ``public_key`` as a JWK tagged with ``kid``."""
    from jwt.algorithms import RSAAlgorithm

    jwk_dict = _json_loads(RSAAlgorithm.to_jwk(public_key))
    jwk_dict["kid"] = kid
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return jwk_dict, kid


def _json_loads(text: str) -> dict[str, Any]:
    import json

    return json.loads(text)


def _sign_token(claims: dict[str, Any], private_key: Any, kid: str) -> str:
    """Sign ``claims`` into an RS256 JWT carrying ``kid`` in the header."""
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _jwks_handler(jwk_dict: dict[str, Any]):
    """Build an httpx MockTransport handler serving a single-key JWKS doc."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/jwks") or "jwks" in str(request.url)
        return httpx.Response(200, json={"keys": [jwk_dict]})

    return handler


def _claims(**overrides: Any) -> dict[str, Any]:
    """Baseline valid ID-token claims, overridable per scenario."""
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-123",
        "exp": now + 3600,
        "iat": now,
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice Example",
        "preferred_username": "alice",
    }
    base.update(overrides)
    return base


def _build_provider(*, transport: httpx.AsyncBaseTransport, **overrides: Any) -> OIDCProvider:
    """Construct an OIDCProvider pointed at a mocked JWKS endpoint."""
    kwargs: dict[str, Any] = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "jwks_uri": f"{ISSUER}/jwks",
        "transport": transport,
    }
    kwargs.update(overrides)
    return OIDCProvider(**kwargs)


# --- Headline happy-path test ---------------------------------------------
async def test_verify_id_token_happy_path_mocked_jwks():
    """End-to-end: fetch JWKS over mocked httpx, then verify a signed token.

    This is the canonical happy path required by the issue: a JWKS endpoint is
    mocked, the provider fetches it with httpx, and an ID token signed by the
    corresponding private key is verified -- including iss/aud/exp checks.
    """
    private_key, public_key = _generate_rsa_keypair()
    jwk_dict, kid = _make_jwk(public_key)

    transport = MockTransport(_jwks_handler(jwk_dict))
    provider = _build_provider(transport=transport)
    # Sanity: it satisfies the shared provider interface like the others.
    assert isinstance(provider, IOAuthProvider)
    assert provider.name == "oidc"

    token = _sign_token(_claims(), private_key, kid)

    # 1. Fetch JWKS via httpx against the mocked endpoint.
    jwks = await provider.fetch_jwks()
    assert isinstance(jwks, dict)
    assert jwks["keys"][0]["kid"] == kid

    # Second fetch is served from cache (the mock handler would still answer,
    # but the cache guarantees one network round-trip regardless).
    jwks_again = await provider.fetch_jwks()
    assert jwks_again is jwks

    # 2. Verify the ID token against the cached JWKS keys.
    verified = provider.verify_id_token(token)
    assert isinstance(verified, IDTokenClaims)
    assert verified.iss == ISSUER
    assert verified.aud == CLIENT_ID
    assert verified.sub == "user-123"
    assert verified.email == "alice@example.com"
    assert verified.email_verified is True
    assert verified.name == "Alice Example"
    assert verified.preferred_username == "alice"
    assert verified.exp > verified.iat
    # The full raw payload is preserved for callers needing unmodelled claims.
    assert verified.raw["email"] == "alice@example.com"


# --- Security-reclaim negative tests --------------------------------------
async def test_verify_id_token_rejects_wrong_issuer():
    private_key, public_key = _generate_rsa_keypair()
    jwk_dict, kid = _make_jwk(public_key)
    transport = MockTransport(_jwks_handler(jwk_dict))
    provider = _build_provider(transport=transport)
    await provider.fetch_jwks()

    token = _sign_token(_claims(iss="https://attacker.example.com"), private_key, kid)
    with pytest.raises(OIDCInvalidTokenError, match="wrong issuer"):
        provider.verify_id_token(token)
    # The provider-specific error is also catchable as the shared base.
    with pytest.raises(BaseInvalidTokenError):
        provider.verify_id_token(token)


async def test_verify_id_token_rejects_wrong_audience():
    private_key, public_key = _generate_rsa_keypair()
    jwk_dict, kid = _make_jwk(public_key)
    transport = MockTransport(_jwks_handler(jwk_dict))
    provider = _build_provider(transport=transport)
    await provider.fetch_jwks()

    token = _sign_token(_claims(aud="some-other-client"), private_key, kid)
    with pytest.raises(OIDCInvalidTokenError, match="wrong audience"):
        provider.verify_id_token(token)


async def test_verify_id_token_rejects_expired_token():
    private_key, public_key = _generate_rsa_keypair()
    jwk_dict, kid = _make_jwk(public_key)
    transport = MockTransport(_jwks_handler(jwk_dict))
    provider = _build_provider(transport=transport)
    await provider.fetch_jwks()

    now = int(time.time())
    token = _sign_token(_claims(exp=now - 60, iat=now - 120), private_key, kid)
    with pytest.raises(OIDCInvalidTokenError, match="expired"):
        provider.verify_id_token(token)


async def test_verify_id_token_rejects_alg_none():
    _private_key, public_key = _generate_rsa_keypair()
    jwk_dict, kid = _make_jwk(public_key)
    transport = MockTransport(_jwks_handler(jwk_dict))
    provider = _build_provider(transport=transport)
    await provider.fetch_jwks()

    # An unsigned (alg=none) token must NEVER verify, regardless of the key.
    now = int(time.time())
    unsigned = jwt.encode(_claims(exp=now + 3600), key="", algorithm="none", headers={"kid": kid})
    with pytest.raises(OIDCInvalidTokenError, match="unsupported signing algorithm"):
        provider.verify_id_token(unsigned)


async def test_verify_id_token_rejects_unknown_kid():
    private_key, public_key = _generate_rsa_keypair()
    jwk_dict, _kid = _make_jwk(public_key)
    transport = MockTransport(_jwks_handler(jwk_dict))
    provider = _build_provider(transport=transport)
    await provider.fetch_jwks()

    # Sign with a kid that is not present in the published JWKS.
    token = _sign_token(_claims(), private_key, kid="missing-kid")
    with pytest.raises(OIDCInvalidTokenError, match="no JWKS key matched"):
        provider.verify_id_token(token)


async def test_verify_id_token_requires_jwks_when_none_available():
    provider = _build_provider(
        transport=MockTransport(lambda r: httpx.Response(200, json={"keys": []}))
    )
    private_key, public_key = _generate_rsa_keypair()
    _, kid = _make_jwk(public_key)
    token = _sign_token(_claims(), private_key, kid)
    with pytest.raises(OIDCInvalidTokenError, match="no JWKS available"):
        provider.verify_id_token(token)


# --- JWKS fetching edge cases ---------------------------------------------
async def test_fetch_jwks_rejects_non_https_remote_url():
    provider = OIDCProvider(
        issuer="http://id.example.com",  # insecure scheme, remote host
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        jwks_uri="http://id.example.com/jwks",
        transport=MockTransport(lambda r: httpx.Response(200, json={"keys": []})),
    )
    with pytest.raises(OIDCError, match="HTTPS"):
        await provider.fetch_jwks()


async def test_fetch_jwks_allows_localhost_http():
    jwk_dict, _ = _make_jwk(_generate_rsa_keypair()[1])
    provider = OIDCProvider(
        issuer="http://localhost:9001",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        jwks_uri="http://localhost:9001/jwks",
        transport=MockTransport(_jwks_handler(jwk_dict)),
    )
    jwks = await provider.fetch_jwks()
    assert jwks["keys"][0]["kid"] == jwk_dict["kid"]


async def test_fetch_jwks_raises_discovery_error_on_http_500():
    provider = _build_provider(
        transport=MockTransport(lambda r: httpx.Response(500, text="boom")),
    )
    with pytest.raises(DiscoveryError, match="HTTP 500"):
        await provider.fetch_jwks()


async def test_fetch_jwks_raises_discovery_error_on_missing_keys():
    provider = _build_provider(
        transport=MockTransport(lambda r: httpx.Response(200, json={"not_keys": []})),
    )
    with pytest.raises(DiscoveryError, match="missing a 'keys' array"):
        await provider.fetch_jwks()


# --- Provider interface / factory -----------------------------------------
def test_get_authorize_url_embeds_state_and_client_id():
    provider = OIDCProvider(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
    )
    url = provider.get_authorize_url(state="csrf-token-xyz")
    assert url.startswith(f"{ISSUER}/authorize?")
    assert "client_id=nexus-client" in url
    assert "response_type=code" in url
    assert "state=csrf-token-xyz" in url
    assert "scope=openid+email+profile" in url or "scope=openid" in url


def test_get_authorize_url_requires_state():
    provider = OIDCProvider(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
    )
    with pytest.raises(OIDCError, match="state is required"):
        provider.get_authorize_url(state="")


def test_state_roundtrip_validate_matches():
    state = OIDCProvider.generate_state()
    # An echoed-equal state must validate without raising.
    OIDCProvider.validate_state(state, state)
    # A mismatched state must raise the OIDC-typed error.
    with pytest.raises(OIDCError):
        OIDCProvider.validate_state(state, "different")


def test_get_oauth_provider_returns_oidc_when_configured(monkeypatch):
    from engine.auth import oidc as oidc_module
    from engine.config import Settings

    settings = Settings(
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET,
        oidc_redirect_uri=REDIRECT_URI,
        oidc_jwks_uri=f"{ISSUER}/jwks",
    )
    monkeypatch.setattr(oidc_module, "settings", settings, raising=False)
    # The factory imports settings lazily from engine.config, so patch there too.
    import engine.config as config_module

    monkeypatch.setattr(config_module, "settings", settings, raising=False)

    provider = get_oauth_provider("oidc")
    assert isinstance(provider, OIDCProvider)
    assert provider.issuer == ISSUER
    assert provider.client_id == CLIENT_ID
    assert provider.jwks_uri == f"{ISSUER}/jwks"


def test_get_oauth_provider_returns_none_when_oidc_unconfigured():
    import engine.config as config_module

    # Default Settings() has empty oidc_issuer -> provider unavailable.
    if config_module.settings.oidc_issuer:
        pytest.skip("environment configures an OIDC issuer; cannot assert default")
    assert get_oauth_provider("oidc") is None


def test_get_oauth_provider_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown OAuth provider"):
        get_oauth_provider("definitely-not-a-real-provider")


# --- Token exchange (mocked token endpoint) -------------------------------
async def test_exchange_code_happy_path():
    private_key, public_key = _generate_rsa_keypair()
    _jwk_dict, kid = _make_jwk(public_key)
    id_token = _sign_token(_claims(), private_key, kid)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/token")
        body = request.read().decode()
        assert "grant_type=authorization_code" in body
        assert f"client_id={CLIENT_ID}" in body
        return httpx.Response(
            200,
            json={
                "access_token": "at-123",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": id_token,
            },
        )

    provider = _build_provider(transport=MockTransport(handler))
    token_set = await provider.exchange_code("the-auth-code")
    assert token_set.access_token == "at-123"
    # The shared base TokenSet keeps provider-specific fields (like the OIDC
    # id_token) in ``raw`` rather than modelling them.
    assert token_set.raw.get("id_token") == id_token
    assert token_set.expires_in == 3600


async def test_exchange_code_error_on_http_400():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    provider = _build_provider(transport=MockTransport(handler))
    with pytest.raises(OIDCTokenExchangeError, match="HTTP 400"):
        await provider.exchange_code("bad-code")
