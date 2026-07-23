"""Unit tests for ``engine/auth/github.py`` -- the GitHub OAuth2 provider.

These tests exercise every discrete step of the authorization-code flow:

* authorization-URL generation (parameter encoding, CSRF ``state`` requirement)
* CSRF ``state`` generation / constant-time validation
* authorization-code -> access-token exchange (``exchange_code``)
* access-token validation against ``/user`` (``validate_access_token``)
* the ``/user/emails`` fallback for users with a private primary email
* profile -> Nexus user-model mapping (``map_user``)
* the exception hierarchy (provider-specific + shared bases)

GitHub's API is fully stubbed with :class:`httpx.MockTransport`, so no test
ever touches the network. Each public endpoint is reachable at a distinct
host/path, so a single routing handler dispatches by URL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from engine.auth.base import (
    InvalidTokenError as BaseInvalidTokenError,
)
from engine.auth.base import (
    IOAuthProvider,
)
from engine.auth.base import (
    OAuthError as BaseOAuthError,
)
from engine.auth.base import (
    TokenExchangeError as BaseTokenExchangeError,
)
from engine.auth.base import TokenSet as BaseTokenSet
from engine.auth.github import (
    GitHubOAuthError,
    GitHubOAuthProvider,
    GitHubUserInfo,
    InvalidTokenError,
    TokenExchangeError,
)

# --- Endpoint URLs (mirrors the constants in engine/auth/github.py) ---------
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_EMAILS_URL = "https://api.github.com/user/emails"

_CLIENT_ID = "gh-client-id"
_CLIENT_SECRET = "gh-client-secret"
_REDIRECT_URI = "https://app.example.com/api/v1/auth/github/callback"


# --- Helpers ----------------------------------------------------------------
def _provider(transport: httpx.MockTransport) -> GitHubOAuthProvider:
    """Build a provider whose HTTP layer is the supplied mock transport."""
    return GitHubOAuthProvider(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        redirect_uri=_REDIRECT_URI,
        transport=transport,
    )


def _transport(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> httpx.MockTransport:
    """Build a MockTransport that dispatches each request by its full URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in routes:
            return httpx.Response(404, text=f"unexpected request to {key}")
        return routes[key](request)

    return httpx.MockTransport(handler)


def _token_handler(body: dict[str, Any], status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        # Token exchange is a form POST; assert the expected fields are sent.
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form.get("client_id") == _CLIENT_ID
        assert form.get("client_secret") == _CLIENT_SECRET
        assert form.get("redirect_uri") == _REDIRECT_URI
        assert form.get("code") == "valid-code"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(status, json=body)

    return handler


def _user_handler(body: dict[str, Any], status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(status, json=body)

    return handler


_FULL_PROFILE = {
    "id": 12345,
    "login": "octocat",
    "name": "The Octocat",
    "email": "octo@example.com",
    "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
}

_TOKEN_BODY = {
    "access_token": "gho_abcdef",
    "token_type": "bearer",
    "scope": "read:user user:email",
}


# ===========================================================================
# Authorization URL
# ===========================================================================
class TestAuthorizeUrl:
    def test_builds_url_with_required_params(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            redirect_uri=_REDIRECT_URI,
        )
        url = provider.get_authorize_url(state="csrf-state-123")
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "github.com"
        assert parsed.path == "/login/oauth/authorize"
        params = parse_qs(parsed.query)
        assert params["client_id"] == [_CLIENT_ID]
        assert params["redirect_uri"] == [_REDIRECT_URI]
        assert params["state"] == ["csrf-state-123"]
        # default scope requests profile + email
        assert "read:user" in params["scope"][0]
        assert "user:email" in params["scope"][0]

    def test_custom_scope_is_passed_through(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        url = provider.get_authorize_url(state="s", scope="repo gist")
        assert parse_qs(urlparse(url).query)["scope"] == ["repo gist"]

    def test_percent_encodes_redirect_uri(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            redirect_uri="https://app.example.com/cb?next=/dash",
        )
        url = provider.get_authorize_url(state="s")
        # The query value must be percent-encoded, not contain a raw '?'
        assert "redirect_uri=https%3A%2F%2Fapp.example.com%2Fcb%3Fnext%3D%2Fdash" in url

    def test_empty_state_is_rejected(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        with pytest.raises(GitHubOAuthError, match="state is required"):
            provider.get_authorize_url(state="")


# ===========================================================================
# get_authorize_url_with_state -- the canonical ``(url, state)`` accessor
# ===========================================================================
class TestAuthorizeUrlWithState:
    """The canonical tuple-returning accessor on :class:`GitHubOAuthProvider`.

    ``get_authorize_url`` returns only the URL string and *requires* a
    non-empty ``state``; ``get_authorize_url_with_state`` is the safe default
    that always embeds a CSRF token (auto-generating one when none is supplied)
    and returns it alongside the URL so the caller can persist and validate it.
    """

    def test_with_supplied_state_returns_url_and_same_state(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            redirect_uri=_REDIRECT_URI,
        )
        url, state = provider.get_authorize_url_with_state(state="csrf-state-123")
        # Interface contract: a 2-tuple of ``(str, str)``.
        assert isinstance((url, state), tuple)
        assert isinstance(url, str)
        assert isinstance(state, str)
        assert state == "csrf-state-123"
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert parsed.path == "/login/oauth/authorize"
        assert params["client_id"] == [_CLIENT_ID]
        assert params["redirect_uri"] == [_REDIRECT_URI]
        assert params["state"] == ["csrf-state-123"]
        assert "read:user" in params["scope"][0]
        assert "user:email" in params["scope"][0]

    def test_without_state_auto_generates_nonempty_csrf_token(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        url, state = provider.get_authorize_url_with_state()
        assert isinstance(url, str) and isinstance(state, str)
        # The auto-generated state is always present and non-empty...
        assert state
        # ...and it is the exact value embedded in the URL (so the caller can
        # round-trip it).
        assert parse_qs(urlparse(url).query)["state"] == [state]

    def test_empty_state_auto_generates_nonempty_csrf_token(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        url, state = provider.get_authorize_url_with_state(state="")
        assert state  # never stateless
        assert parse_qs(urlparse(url).query)["state"] == [state]

    def test_each_auto_generated_state_is_unique(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        _, a = provider.get_authorize_url_with_state()
        _, b = provider.get_authorize_url_with_state()
        assert a and b and a != b

    def test_custom_scope_is_passed_through(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        url, _ = provider.get_authorize_url_with_state(state="s", scope="repo gist")
        assert parse_qs(urlparse(url).query)["scope"] == ["repo gist"]

    def test_two_calls_with_distinct_states_yield_distinct_urls(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        url_a, _ = provider.get_authorize_url_with_state(state="a")
        url_b, _ = provider.get_authorize_url_with_state(state="b")
        assert url_a != url_b


# ===========================================================================
# CSRF state
# ===========================================================================
class TestStateHandling:
    def test_generate_state_is_unique_and_nonempty(self):
        a = GitHubOAuthProvider.generate_state()
        b = GitHubOAuthProvider.generate_state()
        assert a and b
        assert a != b

    def test_validate_state_accepts_match(self):
        state = GitHubOAuthProvider.generate_state()
        GitHubOAuthProvider.validate_state(state, state)  # no raise

    def test_validate_state_rejects_mismatch(self):
        with pytest.raises(GitHubOAuthError, match="mismatch"):
            GitHubOAuthProvider.validate_state("aaa", "bbb")

    def test_validate_state_rejects_missing(self):
        with pytest.raises(GitHubOAuthError, match="missing state"):
            GitHubOAuthProvider.validate_state(None, "abc")

    def test_validate_state_rejects_empty(self):
        with pytest.raises(GitHubOAuthError, match="empty state"):
            GitHubOAuthProvider.validate_state("", "")


# ===========================================================================
# exchange_code
# ===========================================================================
class TestExchangeCode:
    async def test_happy_path_returns_token_set(self):
        transport = _transport({_TOKEN_URL: _token_handler(_TOKEN_BODY)})
        tokens = await _provider(transport).exchange_code("valid-code")
        assert isinstance(tokens, BaseTokenSet)
        assert tokens.access_token == "gho_abcdef"
        assert tokens.token_type == "bearer"
        assert tokens.scope == "read:user user:email"
        assert tokens.raw == _TOKEN_BODY

    async def test_empty_code_raises(self):
        transport = _transport({_TOKEN_URL: _token_handler(_TOKEN_BODY)})
        with pytest.raises(TokenExchangeError, match="authorization code is required"):
            await _provider(transport).exchange_code("")

    async def test_network_error_wrapped(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS down", request=_request)

        transport = _transport({_TOKEN_URL: handler})
        with pytest.raises(TokenExchangeError, match="network error"):
            await _provider(transport).exchange_code("valid-code")

    async def test_http_error_status_wrapped(self):
        transport = _transport(
            {
                _TOKEN_URL: _token_handler(
                    {"error": "bad_verification_code"}, status=400
                )
            }
        )
        with pytest.raises(TokenExchangeError, match="HTTP 400"):
            await _provider(transport).exchange_code("valid-code")

    async def test_non_json_body_wrapped(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json", headers={"content-type": "text/plain"})

        transport = _transport({_TOKEN_URL: handler})
        with pytest.raises(TokenExchangeError, match="non-JSON"):
            await _provider(transport).exchange_code("valid-code")

    async def test_missing_access_token_wrapped(self):
        transport = _transport(
            {_TOKEN_URL: _token_handler({"some": "thing"}, status=200)}
        )
        with pytest.raises(TokenExchangeError, match="missing access_token"):
            await _provider(transport).exchange_code("valid-code")


# ===========================================================================
# validate_access_token
# ===========================================================================
class TestValidateAccessToken:
    async def test_happy_path_with_public_email(self):
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(_FULL_PROFILE),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert isinstance(info, GitHubUserInfo)
        assert info.id == "12345"
        assert info.login == "octocat"
        assert info.email == "octo@example.com"
        assert info.name == "The Octocat"
        assert info.avatar_url == _FULL_PROFILE["avatar_url"]
        assert info.raw == _FULL_PROFILE

    async def test_email_fallback_uses_user_emails_endpoint(self):
        # /user returns a null email (the common private-email case).
        profile = {**_FULL_PROFILE, "email": None}
        emails = [
            {"email": "octo+secondary@example.com", "primary": False, "verified": True},
            {"email": "octo@primary.example.com", "primary": True, "verified": True},
        ]
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: _user_handler(emails),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        # The primary *verified* address wins.
        assert info.email == "octo@primary.example.com"

    async def test_email_fallback_picks_any_verified_when_no_primary(self):
        profile = {**_FULL_PROFILE, "email": None}
        emails = [
            {"email": "unverified@example.com", "primary": True, "verified": False},
            {"email": "verified@example.com", "primary": False, "verified": True},
        ]
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: _user_handler(emails),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.email == "verified@example.com"

    async def test_email_fallback_synthesizes_noreply_when_unresolvable(self):
        profile = {**_FULL_PROFILE, "email": None}
        # /user/emails returns an empty list -> cannot resolve a real address.
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: _user_handler([]),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.email == "octocat@users.noreply.github.com"

    async def test_email_fallback_tolerates_emails_network_error(self):
        # /user/emails is unreachable -> best-effort None -> noreply synthesis.
        profile = {**_FULL_PROFILE, "email": None}

        def emails_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: emails_handler,
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.email == "octocat@users.noreply.github.com"

    async def test_email_fallback_tolerates_emails_http_error(self):
        profile = {**_FULL_PROFILE, "email": None}
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: _user_handler({"message": "forbidden"}, status=403),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.email == "octocat@users.noreply.github.com"

    async def test_email_fallback_tolerates_emails_non_json(self):
        profile = {**_FULL_PROFILE, "email": None}

        def emails_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>nope</html>")

        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: emails_handler,
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.email == "octocat@users.noreply.github.com"

    async def test_email_fallback_tolerates_emails_non_list_payload(self):
        # A malformed (non-list) emails body -> best-effort None.
        profile = {**_FULL_PROFILE, "email": None}
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
            _EMAILS_URL: _user_handler({"unexpected": "shape"}),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.email == "octocat@users.noreply.github.com"

    async def test_empty_token_rejected(self):
        with pytest.raises(InvalidTokenError, match="access token is required"):
            await _provider(_transport({})).validate_access_token("")

    async def test_unauthorized_rejected(self):
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler({"message": "Bad credentials"}, status=401),
        }
        with pytest.raises(InvalidTokenError, match="invalid or expired"):
            await _provider(_transport(routes)).validate_access_token("gho_token")

    async def test_other_http_error_rejected(self):
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler({"message": "rate limited"}, status=403),
        }
        with pytest.raises(InvalidTokenError, match="HTTP 403"):
            await _provider(_transport(routes)).validate_access_token("gho_token")

    async def test_incomplete_profile_missing_id_rejected(self):
        profile = {"login": "octocat", "name": "Octo"}  # no id
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
        }
        with pytest.raises(InvalidTokenError, match="incomplete"):
            await _provider(_transport(routes)).validate_access_token("gho_token")

    async def test_incomplete_profile_missing_login_rejected(self):
        profile = {"id": 12345, "name": "Octo"}  # no login
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
        }
        with pytest.raises(InvalidTokenError, match="incomplete"):
            await _provider(_transport(routes)).validate_access_token("gho_token")

    async def test_non_json_user_body_rejected(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>oops</html>")

        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: handler,
        }
        with pytest.raises(InvalidTokenError, match="non-JSON"):
            await _provider(_transport(routes)).validate_access_token("gho_token")

    async def test_network_error_on_user_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: handler,
        }
        with pytest.raises(InvalidTokenError, match="network error"):
            await _provider(_transport(routes)).validate_access_token("gho_token")

    async def test_display_name_falls_back_to_login_when_name_absent(self):
        profile = {**_FULL_PROFILE, "name": None}
        routes = {
            _TOKEN_URL: _token_handler(_TOKEN_BODY),
            _USER_URL: _user_handler(profile),
        }
        info = await _provider(_transport(routes)).validate_access_token("gho_token")
        assert info.name == "octocat"


# ===========================================================================
# map_user
# ===========================================================================
class TestMapUser:
    def test_maps_profile_to_user_model_shape(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GitHubUserInfo(
            id="42", login="octocat", email="octo@example.com", name="The Octocat"
        )
        mapped = provider.map_user(info)
        assert mapped == {
            "external_id": "42",
            "provider": "github",
            "email": "octo@example.com",
            "display_name": "The Octocat",
            "roles": ["user"],
        }

    def test_display_name_falls_back_to_login(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        info = GitHubUserInfo(id="42", login="octocat", name="")
        mapped = provider.map_user(info)
        assert mapped["display_name"] == "octocat"


# ===========================================================================
# Exception hierarchy / interface conformance
# ===========================================================================
class TestHierarchyAndProtocol:
    def test_token_exchange_error_is_shared_base(self):
        assert issubclass(TokenExchangeError, BaseTokenExchangeError)
        assert issubclass(TokenExchangeError, GitHubOAuthError)

    def test_invalid_token_error_is_shared_base(self):
        assert issubclass(InvalidTokenError, BaseInvalidTokenError)
        assert issubclass(InvalidTokenError, GitHubOAuthError)

    def test_provider_errors_are_oauth_errors(self):
        assert issubclass(GitHubOAuthError, BaseOAuthError)

    def test_shared_base_catches_provider_variant(self):
        # A caller catching the shared base handles the provider subclass too.
        try:
            raise InvalidTokenError("boom")
        except BaseInvalidTokenError:
            pass
        else:
            pytest.fail("shared InvalidTokenError did not catch GitHub variant")

    def test_satisfies_ioauthprovider_protocol(self):
        provider = GitHubOAuthProvider(
            client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
        )
        assert isinstance(provider, IOAuthProvider)
        assert provider.name == "github"
