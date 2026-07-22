"""Focused tests for the OIDC provider's PKCE (RFC 7636) support.

Covers, in isolation:

* :func:`engine.auth.oidc.generate_pkce_pair` -- verifier generation
  (length, charset, uniqueness, S256 challenge derivation).
* :func:`engine.auth.oidc._derive_code_challenge` -- independent recomputation
  of the S256 challenge.
* :meth:`OIDCProvider.get_authorize_url` -- PKCE params in the authorize URL
  and the verifier being **returned** (alongside the URL, in an
  :class:`~engine.auth.oidc.AuthURL`) rather than stashed on the instance,
  both for auto-generated and caller-supplied verifiers.
* :meth:`OIDCProvider.exchange_code` -- the ``code_verifier`` is a required
  argument and is forwarded verbatim in the token request body. The provider
  instance never retains the verifier (it is stateless), so the caller must
  thread the verifier returned by ``get_authorize_url`` back into
  ``exchange_code`` -- typically across an authorization redirect.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from typing import Any

import httpx
import pytest
from httpx import MockTransport

from engine.auth import generate_pkce_pair as pkg_generate_pkce_pair
from engine.auth.oidc import (
    _PKCE_CHALLENGE_METHOD,
    _PKCE_VERIFIER_BYTES,
    AuthURL,
    OIDCError,
    OIDCProvider,
    TokenExchangeError,
    _derive_code_challenge,
    generate_pkce_pair,
)

ISSUER = "https://id.example.com"
CLIENT_ID = "nexus-client"
CLIENT_SECRET = "super-secret"
REDIRECT_URI = "https://app.example.com/callback"

# RFC 7636 permitted ``code_verifier`` characters: the unreserved set.
_UNRESERVED = re.compile(r"^[A-Za-z0-9._~-]+$")


def _provider(transport: httpx.AsyncBaseTransport | None = None, **overrides: Any) -> OIDCProvider:
    kwargs: dict[str, Any] = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }
    if transport is not None:
        kwargs["transport"] = transport
    kwargs.update(overrides)
    return OIDCProvider(**kwargs)


def _independent_s256_challenge(verifier: str) -> str:
    """Recompute the S256 challenge from scratch (independent of the module)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _query(url: str) -> dict[str, str]:
    """Return the query params of ``url`` as a flat dict."""
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def _capturing_handler(sink: dict[str, str], response: dict[str, Any] | None = None):
    """Build a MockTransport handler that captures the form-encoded POST body."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = urllib.parse.parse_qs(request.read().decode())
        sink.update({k: v[0] for k, v in body.items()})
        return httpx.Response(200, json=response or {"access_token": "at", "token_type": "Bearer"})

    return handler


# --- generate_pkce_pair / _derive_code_challenge --------------------------
def test_generate_pkce_pair_returns_verifier_and_challenge():
    verifier, challenge = generate_pkce_pair()

    # Verifier is a non-empty URL-safe string within the RFC 7636 length range.
    assert isinstance(verifier, str) and verifier
    assert _UNRESERVED.match(verifier), f"unexpected verifier charset: {verifier!r}"
    assert 43 <= len(verifier) <= 128, f"verifier length out of spec: {len(verifier)}"

    # Challenge is the independent S256 derivation of the verifier.
    assert challenge == _independent_s256_challenge(verifier)
    # No base64padding leaks into the URL-embedded challenge.
    assert "=" not in challenge


def test_generate_pkce_pair_is_unique_per_call():
    verifiers = {generate_pkce_pair()[0] for _ in range(50)}
    # 50 distinct high-entropy verifiers -- collisions are astronomically
    # unlikely; a collision implies the RNG is broken.
    assert len(verifiers) == 50


def test_generate_pkce_pair_uses_secrets_token_urlsafe_length():
    # ``secrets.token_urlsafe(n)`` yields ceil(n*8/6) characters; pin the
    # configured byte count so the verifier length stays well inside spec.
    verifier, _ = generate_pkce_pair()
    expected_len = -(-(_PKCE_VERIFIER_BYTES * 8) // 6)  # ceil division
    assert len(verifier) == expected_len


def test_derive_code_challenge_matches_known_vectors():
    # Verify against the RFC 7636 Appendix B reference vector.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert _derive_code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generate_pkce_pair_exposed_at_package_level():
    # The helper is re-exported from the package for ergonomic access.
    assert pkg_generate_pkce_pair is generate_pkce_pair
    verifier, challenge = pkg_generate_pkce_pair()
    assert challenge == _independent_s256_challenge(verifier)


# --- get_authorize_url: PKCE params + verifier returned (not stored) -------
def test_get_authorize_url_includes_pkce_params_and_returns_verifier():
    provider = _provider()

    result = provider.get_authorize_url(state="csrf-token-xyz")
    params = _query(result.url)

    # The S256 challenge and method are present in the authorize URL.
    assert "code_challenge" in params
    assert params["code_challenge_method"] == _PKCE_CHALLENGE_METHOD == "S256"
    assert "=" not in params["code_challenge"]

    # The verifier is *returned* (as part of an AuthURL), not stored on the
    # instance, and is the secret counterpart to the embedded challenge.
    assert isinstance(result, AuthURL)
    assert result.code_verifier
    assert _independent_s256_challenge(result.code_verifier) == params["code_challenge"]

    # The provider instance is stateless: it never retains the verifier.
    assert not hasattr(provider, "_code_verifier")

    # Core OAuth2 params are still present.
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["state"] == "csrf-token-xyz"


def test_get_authorize_url_accepts_explicit_code_verifier():
    provider = _provider()
    verifier, challenge = generate_pkce_pair()

    result = provider.get_authorize_url(state="s", code_verifier=verifier)
    params = _query(result.url)

    # The challenge is derived from the caller-supplied verifier (not freshly
    # generated), and that same verifier is echoed back in the AuthURL.
    assert params["code_challenge"] == challenge == _independent_s256_challenge(verifier)
    assert result.code_verifier == verifier
    # Still nothing retained on the instance.
    assert not hasattr(provider, "_code_verifier")


def test_get_authorize_url_regenerates_verifier_per_call():
    provider = _provider()
    first = provider.get_authorize_url(state="s1")
    second = provider.get_authorize_url(state="s2")

    # Each call produces a fresh verifier/challenge pair with no shared state.
    assert first.code_verifier != second.code_verifier
    assert _query(first.url)["code_challenge"] != _query(second.url)["code_challenge"]


def test_get_authorize_url_concurrent_calls_do_not_clobber_verifier():
    # The headline reason the verifier is returned rather than stored: two
    # interleaved authorize requests must keep independent verifiers. With the
    # old instance-storage design the second call would have overwritten the
    # first provider's verifier, breaking the first request's token exchange.
    provider = _provider()
    first = provider.get_authorize_url(state="s1")
    provider.get_authorize_url(state="s2")

    # The first request's verifier still validates against its own URL even
    # after a second request has been started on the same provider instance.
    first_params = _query(first.url)
    assert _independent_s256_challenge(first.code_verifier) == first_params["code_challenge"]


def test_get_authorize_url_pkce_does_not_clobber_state_validation():
    provider = _provider()
    # state is still mandatory even with PKCE in play.
    try:
        provider.get_authorize_url(state="")
    except OIDCError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected OIDCError for empty state")


# --- exchange_code: code_verifier is required + forwarded in the body -----
async def test_exchange_code_sends_code_verifier_in_body():
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))

    verifier = "replayed-verifier-from-session"
    token_set = await provider.exchange_code("the-code", code_verifier=verifier)

    # The caller-supplied verifier is forwarded verbatim.
    assert sent["code_verifier"] == verifier
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert token_set.access_token == "at"


async def test_exchange_code_requires_code_verifier_argument():
    # The provider no longer remembers the verifier between calls, so it MUST
    # be passed in. Omitting the now-required keyword is a TypeError.
    provider = _provider(
        transport=MockTransport(_capturing_handler({})),
    )
    with pytest.raises(TypeError):
        await provider.exchange_code("the-code")  # type: ignore[call-arg]


async def test_exchange_code_rejects_empty_code_verifier():
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))

    # An empty verifier can never match a challenge; rejected up front without
    # hitting the network.
    with pytest.raises(TokenExchangeError, match="code_verifier is required"):
        await provider.exchange_code("the-code", code_verifier="")
    assert sent == {}


async def test_exchange_code_does_not_read_stored_state():
    # Regression guard: even after get_authorize_url() the provider holds no
    # verifier, so a caller that forgets to pass one cannot accidentally
    # succeed by replaying stale instance state.
    provider = _provider(
        transport=MockTransport(_capturing_handler({})),
    )
    provider.get_authorize_url(state="s")
    assert not hasattr(provider, "_code_verifier")
    with pytest.raises(TypeError):
        await provider.exchange_code("the-code")  # type: ignore[call-arg]


# --- End-to-end PKCE round-trip -------------------------------------------
async def test_pkce_round_trip_authorize_then_exchange():
    """The full PKCE flow with the verifier threaded by the caller.

    1. ``get_authorize_url`` generates a verifier, returns it in the
       :class:`AuthURL`, and embeds the matching S256 challenge in the URL.
    2. The caller persists that verifier across the redirect.
    3. ``exchange_code`` replays the caller-supplied verifier in the token
       request body.
    The token endpoint handler asserts the verifier hashes back to the
    challenge that appeared in the authorize URL.
    """
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))

    auth = provider.get_authorize_url(state="roundtrip-state")
    expected_challenge = _query(auth.url)["code_challenge"]

    # The caller threads the returned verifier into the exchange.
    await provider.exchange_code("rc-code", code_verifier=auth.code_verifier)

    # The verifier sent in the exchange hashes to the challenge in the URL.
    assert _independent_s256_challenge(sent["code_verifier"]) == expected_challenge
