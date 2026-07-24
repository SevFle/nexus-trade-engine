"""Unit tests for ``engine/auth/providers/google.py`` -- the Google OAuth2 provider.

These tests exercise the userinfo-endpoint flow that :class:`GoogleOAuthProvider`
exposes for the OAuth2 (access-token) sign-in path:

* :meth:`GoogleOAuthProvider.get_authorize_url` -- authorization-endpoint URL
  construction (parameter encoding, required CSRF ``state``).
* :meth:`GoogleOAuthProvider.exchange_code_for_token` -- the canonical
  authorization-code -> access-token exchange (the ``code-for-token`` step).
* :meth:`GoogleOAuthProvider.get_user_info` -- resolving the access token to a
  normalized :class:`GoogleUserInfo` via the ``oauth2/v2/userinfo`` endpoint.

The centerpiece is a single happy-path test that mocks both the token-exchange
and userinfo HTTP responses and asserts the normalized user fields
(``email``, ``name``, ``provider_id``) the rest of the engine links on.

Google's API is fully stubbed with :class:`httpx.MockTransport`, so no test
ever touches the network. Each endpoint lives at a distinct host/path, so one
routing handler dispatches a request by its full URL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from engine.auth.base import IOAuthProvider
from engine.auth.base import TokenSet as BaseTokenSet
from engine.auth.providers.google import (
    GoogleOAuthError,
    GoogleOAuthProvider,
    GoogleUserInfo,
    InvalidTokenError,
    _coerce_email_verified,
)

# --- Endpoint URLs (mirror the constants in engine/auth/providers/google.py) -
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

_CLIENT_ID = "g-client-id"
_CLIENT_SECRET = "g-client-secret"
_REDIRECT_URI = "https://app.example.com/api/v1/auth/google/callback"


# --- Helpers ----------------------------------------------------------------
def _provider(transport: httpx.MockTransport) -> GoogleOAuthProvider:
    """Build a provider whose HTTP layer is the supplied mock transport."""
    return GoogleOAuthProvider(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        redirect_uri=_REDIRECT_URI,
        transport=transport,
    )


def _transport(
    routes: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> httpx.MockTransport:
    """Build a MockTransport that dispatches each request by its full URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in routes:
            return httpx.Response(404, text=f"unexpected request to {key}")
        return routes[key](request)

    return httpx.MockTransport(handler)


def _token_handler(
    body: dict[str, Any], status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        # Token exchange is a form POST; assert the expected fields are sent.
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form.get("client_id") == _CLIENT_ID
        assert form.get("client_secret") == _CLIENT_SECRET
        assert form.get("redirect_uri") == _REDIRECT_URI
        assert form.get("grant_type") == "authorization_code"
        assert form.get("code") == "valid-code"
        return httpx.Response(status, json=body)

    return handler


def _userinfo_handler(
    body: dict[str, Any], status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        # userinfo is fetched with the access token as a Bearer credential.
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(status, json=body)

    return handler


_TOKEN_BODY: dict[str, Any] = {
    "access_token": "ya29.abcdef",
    "token_type": "Bearer",
    "expires_in": 3599,
    "scope": "openid email profile",
    "id_token": "eyJid.this.is.mocked",
}

_USERINFO_BODY: dict[str, Any] = {
    "sub": "1082147932154367890",
    "email": "ada@example.com",
    "email_verified": True,
    "name": "Ada Lovelace",
    "given_name": "Ada",
    "family_name": "Lovelace",
    "picture": "https://lh3.googleusercontent.com/a-/avatar",
    "locale": "en",
}


# ===========================================================================
# Happy path: authorize URL -> code-for-token -> userinfo normalization
# ===========================================================================
class TestGoogleAuthorizeUrl:
    def test_builds_url_with_required_params(self):
        url = GoogleOAuthProvider(
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            redirect_uri=_REDIRECT_URI,
        ).get_authorize_url(state="csrf-state-123")
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "accounts.google.com"
        assert parsed.path == "/o/oauth2/v2/auth"
        params = parse_qs(parsed.query)
        assert params["client_id"] == [_CLIENT_ID]
        assert params["redirect_uri"] == [_REDIRECT_URI]
        assert params["response_type"] == ["code"]
        assert params["state"] == ["csrf-state-123"]
        # default scope requests openid + email + profile
        assert "openid" in params["scope"][0]
        assert "email" in params["scope"][0]
        assert "profile" in params["scope"][0]


class TestExchangeAndUserInfo:
    async def test_exchange_code_for_token_then_get_user_info_happy_path(self):
        """The headline test: code -> token -> normalized user profile.

        Mocks both the token-exchange and userinfo HTTP responses and asserts
        the normalized user fields (``email``, ``name``, ``provider_id``) the
        rest of the engine links an OAuth-backed user on.
        """
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USERINFO_URL: _userinfo_handler(_USERINFO_BODY),
        }
        provider = _provider(_transport(routes))

        # 1. authorization code -> access token (the "code-for-token" step)
        tokens = await provider.exchange_code_for_token("valid-code")
        assert isinstance(tokens, BaseTokenSet)
        assert tokens.access_token == "ya29.abcdef"
        assert tokens.token_type == "Bearer"
        assert tokens.expires_in == 3599
        assert tokens.id_token == _TOKEN_BODY["id_token"]
        assert tokens.raw == _TOKEN_BODY

        # 2. access token -> normalized, type-safe user profile
        info = await provider.get_user_info(tokens.access_token)
        assert isinstance(info, GoogleUserInfo)
        # --- the normalized fields the task asks us to assert ---
        assert info.provider_id == "1082147932154367890"
        assert info.email == "ada@example.com"
        assert info.name == "Ada Lovelace"
        # plus the extra normalized fields the engine may use
        assert info.email_verified is True
        assert info.avatar_url == _USERINFO_BODY["picture"]
        assert info.raw == _USERINFO_BODY


# ===========================================================================
# get_user_info error contract (mirrors GitHub's validate_access_token)
# ===========================================================================
class TestGetUserInfoErrors:
    async def test_empty_token_rejected(self):
        with pytest.raises(InvalidTokenError, match="access token is required"):
            await _provider(_transport({})).get_user_info("")

    async def test_unauthorized_rejected(self):
        routes = {
            _USERINFO_URL: _userinfo_handler({"error": "invalid_token"}, status=401),
        }
        with pytest.raises(InvalidTokenError, match="invalid or expired"):
            await _provider(_transport(routes)).get_user_info("ya29.token")

    async def test_other_http_error_rejected(self):
        routes = {
            _USERINFO_URL: _userinfo_handler({"error": "rate limited"}, status=403),
        }
        with pytest.raises(InvalidTokenError, match="HTTP 403"):
            await _provider(_transport(routes)).get_user_info("ya29.token")

    async def test_non_json_body_rejected(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>oops</html>")

        routes = {_USERINFO_URL: handler}
        with pytest.raises(InvalidTokenError, match="non-JSON"):
            await _provider(_transport(routes)).get_user_info("ya29.token")

    async def test_network_error_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS down", request=request)

        routes = {_USERINFO_URL: handler}
        with pytest.raises(InvalidTokenError, match="network error"):
            await _provider(_transport(routes)).get_user_info("ya29.token")

    async def test_incomplete_profile_missing_sub_rejected(self):
        # Google always returns ``sub``; a profile without it is malformed and
        # must NOT be linkable to an account.
        profile = {k: v for k, v in _USERINFO_BODY.items() if k != "sub"}
        routes = {_USERINFO_URL: _userinfo_handler(profile)}
        with pytest.raises(InvalidTokenError, match="incomplete"):
            await _provider(_transport(routes)).get_user_info("ya29.token")

    async def test_name_falls_back_to_email_local_part(self):
        profile = {**_USERINFO_BODY}
        del profile["name"]
        routes = {_USERINFO_URL: _userinfo_handler(profile)}
        info = await _provider(_transport(routes)).get_user_info("ya29.token")
        assert info.name == "ada"

    async def test_email_verified_string_false_is_not_truthy(self):
        # Some flows/clients serialize email_verified as the string "false",
        # which bool() would treat as truthy -- an unverified email would
        # masquerade as verified. The provider must normalize it to False.
        profile = {**_USERINFO_BODY, "email_verified": "false"}
        routes = {_USERINFO_URL: _userinfo_handler(profile)}
        info = await _provider(_transport(routes)).get_user_info("ya29.token")
        assert info.email_verified is False

    async def test_email_verified_string_true_is_truthy(self):
        profile = {**_USERINFO_BODY, "email_verified": "true"}
        routes = {_USERINFO_URL: _userinfo_handler(profile)}
        info = await _provider(_transport(routes)).get_user_info("ya29.token")
        assert info.email_verified is True

    async def test_email_verified_missing_defaults_to_false(self):
        profile = {k: v for k, v in _USERINFO_BODY.items() if k != "email_verified"}
        routes = {_USERINFO_URL: _userinfo_handler(profile)}
        info = await _provider(_transport(routes)).get_user_info("ya29.token")
        assert info.email_verified is False


# ===========================================================================
# map_user normalization + protocol conformance
# ===========================================================================
class TestMapUser:
    def test_maps_profile_to_user_model_shape(self):
        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GoogleUserInfo(
            provider_id="42",
            email="ada@example.com",
            name="Ada Lovelace",
            email_verified=True,
        )
        mapped = provider.map_user(info)
        assert mapped == {
            "external_id": "42",
            "provider": "google",
            "email": "ada@example.com",
            "display_name": "Ada Lovelace",
            "email_verified": True,
            "roles": ["user"],
        }

    def test_map_user_surfaces_email_verified_for_account_linking(self):
        # Downstream account-linking gates on a verified email, so map_user()
        # MUST surface the verified flag alongside the rest of the profile.
        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GoogleUserInfo(
            provider_id="42",
            email="ada@example.com",
            name="Ada Lovelace",
            email_verified=True,
        )
        assert provider.map_user(info)["email_verified"] is True

    def test_display_name_falls_back_to_email_local_part(self):
        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GoogleUserInfo(
            provider_id="42",
            email="ada@example.com",
            name="",
            email_verified=True,
        )
        mapped = provider.map_user(info)
        assert mapped["display_name"] == "ada"

    def test_map_user_raises_on_unverified_email(self):
        # Security guard: linking an unverified email would let an attacker
        # assert ownership of an address they do not control, so map_user()
        # MUST refuse such a profile.
        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GoogleUserInfo(
            provider_id="42",
            email="ada@example.com",
            name="Ada Lovelace",
            email_verified=False,
        )
        with pytest.raises(GoogleOAuthError, match="unverified email"):
            provider.map_user(info)

    def test_map_user_unverified_error_is_oauth_error(self):
        # Callers rely on catching the shared OAuthError base for any provider
        # rejection, so the unverified-email guard must derive from it.
        from engine.auth.base import OAuthError

        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GoogleUserInfo(
            provider_id="42", email="ada@example.com", email_verified=False
        )
        with pytest.raises(OAuthError):
            provider.map_user(info)

    def test_map_user_allows_profile_without_email(self):
        # A profile with no email at all is harmless (nothing to impersonate)
        # and must still map -- some sign-ins grant no ``email`` scope.
        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GoogleUserInfo(provider_id="42", email="", name="Ada Lovelace")
        mapped = provider.map_user(info)
        assert mapped["email"] == ""
        assert mapped["display_name"] == "Ada Lovelace"


class TestProtocolConformance:
    def test_satisfies_ioauthprovider_protocol(self):
        provider = GoogleOAuthProvider(
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            redirect_uri=_REDIRECT_URI,
        )
        assert isinstance(provider, IOAuthProvider)
        assert provider.name == "google"


# ===========================================================================
# _coerce_email_verified normalization
# ===========================================================================
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Real booleans pass straight through.
        (True, True),
        (False, False),
        # Lower-case string spellings.
        ("true", True),
        ("false", False),
        # Title-case spellings (some clients serialize this way).
        ("True", True),
        ("False", False),
        # String "0" must NOT be truthy (bool("0") would be True).
        ("0", False),
        # Numeric values fall back to bool().
        (1, True),
        (0, False),
        # None / missing key both collapse to False (the path
        # profile.get("email_verified", False) takes when the claim is absent).
        (None, False),
        # Whitespace + case variants still normalize correctly.
        ("  TRUE ", True),
        ("False\n", False),
    ],
)
def test_coerce_email_verified(value, expected):
    assert _coerce_email_verified(value) is expected
