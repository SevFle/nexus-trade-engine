"""Focused tests for the OIDC provider's PKCE (RFC 7636) support.

Covers, in isolation:

* :func:`engine.auth.oidc.generate_pkce_pair` -- verifier generation
  (length, charset, uniqueness, S256 challenge derivation).
* :func:`engine.auth.oidc._derive_code_challenge` -- independent recomputation
  of the S256 challenge.
* :meth:`OIDCProvider.get_authorize_url` -- PKCE params in the authorize URL
  and verifier capture/storage, both for auto-generated and caller-supplied
  verifiers.
* :meth:`OIDCProvider.exchange_code` -- the ``code_verifier`` is sent in the
  token request body, whether supplied explicitly or replayed from the
  instance state captured by ``get_authorize_url``.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from typing import Any

import httpx
from httpx import MockTransport

from engine.auth import generate_pkce_pair as pkg_generate_pkce_pair
from engine.auth.oidc import (
    _PKCE_CHALLENGE_METHOD,
    _PKCE_VERIFIER_BYTES,
    OIDCError,
    OIDCProvider,
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
    # No base64 padding leaks into the URL-embedded challenge.
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


# --- get_authorize_url: PKCE params + verifier capture --------------------
def test_get_authorize_url_includes_pkce_params_and_stores_verifier():
    provider = _provider()
    assert provider._code_verifier is None

    url = provider.get_authorize_url(state="csrf-token-xyz")
    params = _query(url)

    # The S256 challenge and method are present in the authorize URL.
    assert "code_challenge" in params
    assert params["code_challenge_method"] == _PKCE_CHALLENGE_METHOD == "S256"
    assert "=" not in params["code_challenge"]

    # The stored verifier is the secret counterpart to the embedded challenge.
    stored = provider._code_verifier
    assert stored is not None
    assert _independent_s256_challenge(stored) == params["code_challenge"]

    # Core OAuth2 params are still present.
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["state"] == "csrf-token-xyz"


def test_get_authorize_url_accepts_explicit_code_verifier():
    provider = _provider()
    verifier, challenge = generate_pkce_pair()

    url = provider.get_authorize_url(state="s", code_verifier=verifier)
    params = _query(url)

    # The challenge is derived from the caller-supplied verifier (not freshly
    # generated), and that verifier is the one stashed for later exchange.
    assert params["code_challenge"] == challenge == _independent_s256_challenge(verifier)
    assert provider._code_verifier == verifier


def test_get_authorize_url_regenerates_verifier_per_call():
    provider = _provider()
    first = provider.get_authorize_url(state="s1")
    v1 = provider._code_verifier
    second = provider.get_authorize_url(state="s2")
    v2 = provider._code_verifier

    # Each call produces a fresh challenge/verifier pair.
    assert v1 != v2
    assert _query(first)["code_challenge"] != _query(second)["code_challenge"]


def test_get_authorize_url_pkce_does_not_clobber_state_validation():
    provider = _provider()
    # state is still mandatory even with PKCE in play.
    try:
        provider.get_authorize_url(state="")
    except OIDCError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected OIDCError for empty state")


# --- exchange_code: code_verifier in the token request body ---------------
async def test_exchange_code_replays_stored_code_verifier():
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))
    provider.get_authorize_url(state="s")  # captures a verifier on the instance
    captured_verifier = provider._code_verifier
    assert captured_verifier is not None

    token_set = await provider.exchange_code("the-code")

    assert sent["code_verifier"] == captured_verifier
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert token_set.access_token == "at"


async def test_exchange_code_sends_explicit_code_verifier_over_stored():
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))
    provider.get_authorize_url(state="s")  # populates _code_verifier

    explicit = "explicit-verifier-value"
    await provider.exchange_code("the-code", code_verifier=explicit)

    # An explicit verifier always wins over the stashed one.
    assert sent["code_verifier"] == explicit


async def test_exchange_code_omits_code_verifier_when_none_available():
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))
    # No get_authorize_url() called -> no stored verifier.

    await provider.exchange_code("the-code")

    # Without a verifier anywhere, the param is simply absent (legacy IdPs).
    assert "code_verifier" not in sent


# --- End-to-end PKCE round-trip -------------------------------------------
async def test_pkce_round_trip_authorize_then_exchange():
    """A single provider instance drives the full PKCE flow.

    1. ``get_authorize_url`` generates + stores the verifier and embeds the
       matching S256 challenge in the URL.
    2. ``exchange_code`` replays that exact verifier in the token request body.
    The token endpoint handler asserts the verifier hashes back to the
    challenge that appeared in the authorize URL.
    """
    sent: dict[str, str] = {}
    provider = _provider(transport=MockTransport(_capturing_handler(sent)))

    url = provider.get_authorize_url(state="roundtrip-state")
    expected_challenge = _query(url)["code_challenge"]

    await provider.exchange_code("rc-code")

    # The verifier sent in the exchange hashes to the challenge in the URL.
    assert _independent_s256_challenge(sent["code_verifier"]) == expected_challenge
