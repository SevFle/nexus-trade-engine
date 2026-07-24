"""Tests for the recently-changed authorize-URL handling in
``engine/api/routes/auth.py`` and ``engine/api/auth/github_oauth.py``.

These cover the behaviour introduced by the CSRF-state OAuth work:

* **Route** (:func:`engine.api.routes.auth.authorize_provider`) -- the
  ``inspect.isawaitable`` / ``await`` handling that mirrors the
  ``get_authorize_url`` pattern for the canonical
  ``get_authorize_url_with_state`` accessor. A registry is provider-agnostic,
  so a provider MAY expose either accessor as a *coroutine*; the route must
  await it only when it is actually awaitable and must surface the
  AUTHORITATIVE state the IdP embeds. Also covers the generic
  ``get_authorize_url`` fallback (sync + async, string + tuple returns) and
  the "Could not build authorize URL" 500 path.

* **Adapter** (:class:`engine.api.auth.github_oauth.GitHubAuthProvider`) --
  the ``_normalize_authorize_url`` defence-in-depth guard (accepts only a
  ``str`` or length-2 ``(url, state)`` tuple, raising
  :class:`GitHubOAuthError` for anything malformed), and the
  ``get_authorize_url -> str`` interface contract (returns the URL string
  only, generating the CSRF state internally when none is supplied, while
  ``get_authorize_url_with_state`` returns the typed ``(url, state)`` pair).

All tests are hermetic: the route tests use stub providers behind a mock
registry; the adapter tests inject a stub
:class:`~engine.auth.github.GitHubOAuthProvider` whose ``get_authorize_url``
is a plain :class:`~unittest.mock.MagicMock`.
"""

from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.api.routes.auth import (
    authorize_provider,
    validate_authorize_url,
    validate_oauth_state,
)
from engine.app import create_app
from engine.auth.base import TokenSet
from engine.auth.github import (
    GitHubOAuthError,
    GitHubUserInfo,
)
from engine.config import Settings
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user

_DEFAULT_SCOPE = "read:user user:email"
_TOKEN_SET = TokenSet(access_token="gho_token", token_type="bearer")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _stub_oauth(*, authorize_return="https://github.com/login/oauth/authorize?state=xyz"):
    """A stub :class:`GitHubOAuthProvider` whose ``get_authorize_url`` returns
    ``authorize_return`` (a string by default). The async OAuth2 steps are
    :class:`AsyncMock` instances so the adapter is never networked."""
    provider = MagicMock()
    provider.name = "github"
    provider.exchange_code = AsyncMock(return_value=_TOKEN_SET)
    provider.validate_access_token = AsyncMock(
        return_value=GitHubUserInfo(id="1", login="o", email="o@e.com", name="O")
    )
    provider.get_authorize_url = MagicMock(return_value=authorize_return)
    provider.generate_state = MagicMock(return_value="auto-state-token")
    return provider


def _settings(monkeypatch) -> Settings:
    s = Settings(
        github_client_id="gh-id",
        github_client_secret="gh-secret",
        github_redirect_uri="https://app.example.com/callback",
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


def _boot_app_with_provider(db_session, provider):
    """Create an app whose auth registry returns ``provider`` for every name."""
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = _fake_authenticated_user

    registry = MagicMock()
    registry.get.return_value = provider
    app.state.auth_registry = registry
    return app


def _state_from_url(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("state", [None])[0]


# ===========================================================================
# Route: awaitable get_authorize_url_with_state (the core of focus point 1)
# ===========================================================================
class TestRouteAwaitableWithState:
    """A provider MAY implement ``get_authorize_url_with_state`` as a
    coroutine. The route MUST detect this via ``inspect.isawaitable`` and
    await it, then unpack the ``(url, state)`` tuple exactly as for the
    synchronous case. The AUTHORITATIVE state returned by the provider (which
    may differ from the locally minted one) is what gets persisted in the
    session cookie and surfaced to the caller."""

    @pytest.mark.asyncio
    async def test_awaitable_with_state_is_awaited_and_state_persisted(self, db_session):
        class AsyncStateProvider:
            name = "asyncstate"

            async def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
                # The provider mints its OWN state and embeds it in the URL.
                authoritative = "provider-async-state"
                return f"https://idp.example.com/auth?state={authoritative}", authoritative

        app = _boot_app_with_provider(db_session, AsyncStateProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/asyncstate/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # The provider's authoritative state wins over the route's minted one.
        assert data["state"] == "provider-async-state"
        assert "provider-async-state" in data["authorize_url"]
        # And it is persisted for callback validation.
        assert resp.cookies.get("oauth_state_asyncstate") == "provider-async-state"

    @pytest.mark.asyncio
    async def test_awaitable_with_state_matching_minted_is_unchanged(self, db_session):
        # When the provider echoes back the SAME state the route minted, the
        # surfaced value is unchanged -- the awaitable path behaves like sync.
        class AsyncEchoProvider:
            name = "asyncecho"

            async def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
                return f"https://idp.example.com/auth?state={state}", state

        app = _boot_app_with_provider(db_session, AsyncEchoProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/asyncecho/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] in data["authorize_url"]
        assert resp.cookies.get("oauth_state_asyncecho") == data["state"]


# ===========================================================================
# Route: awaitable get_authorize_url fallback (mirrors the pattern)
# ===========================================================================
class TestRouteAwaitableFallback:
    """Providers without ``get_authorize_url_with_state`` fall back to
    ``get_authorize_url``, which the route also handles as potentially
    awaitable. It accepts either a plain URL string or a ``(url, state)``
    tuple, awaiting first when needed."""

    @pytest.mark.asyncio
    async def test_awaitable_fallback_string(self, db_session):
        class AsyncStringProvider:
            name = "asyncstr"

            async def get_authorize_url(self, state: str = "") -> str:
                return f"https://idp.example.com/auth?state={state}"

        app = _boot_app_with_provider(db_session, AsyncStringProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/asyncstr/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Plain string return keeps the route's minted state.
        assert data["state"]
        assert data["state"] in data["authorize_url"]
        assert resp.cookies.get("oauth_state_asyncstr") == data["state"]

    @pytest.mark.asyncio
    async def test_awaitable_fallback_tuple(self, db_session):
        class AsyncTupleProvider:
            name = "asynctup"

            async def get_authorize_url(self, state: str = "") -> tuple[str, str]:
                authoritative = "fallback-async-state"
                return f"https://idp.example.com/auth?state={authoritative}", authoritative

        app = _boot_app_with_provider(db_session, AsyncTupleProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/asynctup/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "fallback-async-state"
        assert "fallback-async-state" in data["authorize_url"]
        assert resp.cookies.get("oauth_state_asynctup") == "fallback-async-state"

    @pytest.mark.asyncio
    async def test_sync_fallback_tuple_unpacks_state(self, db_session):
        # Synchronous tuple return on the fallback path exercises the
        # ``isinstance(maybe_url, tuple)`` branch (the awaitable branch is
        # skipped because the result is not awaitable).
        class SyncTupleProvider:
            name = "synctup"

            def get_authorize_url(self, state: str = "") -> tuple[str, str]:
                authoritative = "sync-tuple-state"
                return f"https://idp.example.com/auth?state={authoritative}", authoritative

        app = _boot_app_with_provider(db_session, SyncTupleProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/synctup/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "sync-tuple-state"
        assert resp.cookies.get("oauth_state_synctup") == "sync-tuple-state"


# ===========================================================================
# Route: error path -- no URL could be built
# ===========================================================================
class TestRouteNoUrlBuilt:
    @pytest.mark.asyncio
    async def test_provider_without_url_accessors_returns_500(self, db_session):
        # A provider exposing neither accessor leaves ``url`` empty -> 500.
        class SilentProvider:
            name = "silent"

        app = _boot_app_with_provider(db_session, SilentProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/silent/authorize")

        assert resp.status_code == 500
        assert "authorize URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_empty_string_url_returns_500(self, db_session):
        class EmptyUrlProvider:
            name = "emptyurl"

            def get_authorize_url(self, state: str = "") -> str:
                return ""

        app = _boot_app_with_provider(db_session, EmptyUrlProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/emptyurl/authorize")

        assert resp.status_code == 500
        assert "authorize URL" in resp.json()["detail"]


# ===========================================================================
# Adapter: _normalize_authorize_url defence-in-depth guard
# ===========================================================================
class TestNormalizeAuthorizeUrl:
    """``_normalize_authorize_url`` accepts only a ``str`` or a length-2
    ``(url, state)`` tuple and raises :class:`GitHubOAuthError` for anything
    else, so a malformed/monkey-patched provider can never emit a bad URL to a
    user's browser. Tested both directly (it is a static method) and through
    the public ``get_authorize_url_with_state`` accessor."""

    def test_accepts_plain_string(self):
        url = GitHubAuthProvider._normalize_authorize_url("https://idp/auth?state=x")
        assert url == "https://idp/auth?state=x"

    def test_accepts_two_tuple_extracts_url(self):
        url = GitHubAuthProvider._normalize_authorize_url(("https://idp/auth", "state-token"))
        assert url == "https://idp/auth"

    def test_rejects_tuple_of_wrong_length_too_short(self):
        with pytest.raises(GitHubOAuthError) as exc:
            GitHubAuthProvider._normalize_authorize_url(("only-one",))
        assert "length 1" in str(exc.value)

    def test_rejects_tuple_of_wrong_length_too_long(self):
        with pytest.raises(GitHubOAuthError) as exc:
            GitHubAuthProvider._normalize_authorize_url(("a", "b", "c"))
        assert "length 3" in str(exc.value)

    @pytest.mark.parametrize("bad", [123, 4.5, None, object(), ["a", "b"]])
    def test_rejects_non_string_non_tuple(self, bad):
        with pytest.raises(GitHubOAuthError) as exc:
            GitHubAuthProvider._normalize_authorize_url(bad)
        assert "must be a str or (url, state) tuple" in str(exc.value)

    def test_rejects_non_string_url_inside_tuple(self):
        with pytest.raises(GitHubOAuthError) as exc:
            GitHubAuthProvider._normalize_authorize_url((123, "state"))
        assert "authorize URL must be a string" in str(exc.value)

    def test_guard_fires_through_public_accessor(self):
        # The guard is wired through ``get_authorize_url_with_state``: a
        # provider returning a malformed tuple surfaces the typed error rather
        # than silently producing a bad URL.
        oauth = _stub_oauth(authorize_return=("too", "short", "tuple"))
        adapter = GitHubAuthProvider(oauth_provider=oauth)
        with pytest.raises(GitHubOAuthError):
            adapter.get_authorize_url_with_state(state="x")


# ===========================================================================
# Adapter: get_authorize_url -> str contract + state generation
# ===========================================================================
class TestAdapterAuthorizeUrlContract:
    """``get_authorize_url`` honours the ``-> str`` interface contract shared
    with every other registry provider (Google/OIDC), returning the URL string
    only. ``get_authorize_url_with_state`` is the typed ``(url, state)`` pair.
    A CSRF state is ALWAYS embedded -- generated internally when none is
    supplied."""

    def test_get_authorize_url_returns_str_not_tuple(self):
        oauth = _stub_oauth()
        url = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url(state="abc")
        assert isinstance(url, str)
        assert not isinstance(url, tuple)

    def test_get_authorize_url_with_state_returns_tuple(self):
        oauth = _stub_oauth()
        result = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url_with_state(
            state="abc"
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(part, str) for part in result)

    def test_supplied_state_is_not_regenerated(self):
        # When the caller supplies a state, the adapter must NOT call
        # ``generate_state`` -- it round-trips the supplied value.
        oauth = _stub_oauth()
        _url, state = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url_with_state(
            state="caller-state"
        )
        oauth.generate_state.assert_not_called()
        assert state == "caller-state"

    def test_missing_state_is_generated_and_round_trips(self, monkeypatch):
        # No state supplied -> a state is generated internally and the SAME
        # value is both embedded in the URL (via the underlying provider) and
        # returned to the caller.
        _settings(monkeypatch)
        embedded = []

        def _build_url(*, state, scope=_DEFAULT_SCOPE):
            embedded.append(state)
            return f"https://github.com/login/oauth/authorize?state={state}"

        oauth = _stub_oauth()
        oauth.get_authorize_url = MagicMock(side_effect=_build_url)
        url, state = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url_with_state()

        oauth.generate_state.assert_called_once()
        assert state == "auto-state-token"
        # The returned state is exactly the one embedded in the URL.
        assert _state_from_url(url) == state == embedded[0]

    def test_get_authorize_url_without_state_still_embeds_state(self, monkeypatch):
        # ``get_authorize_url()`` (no args) must never produce a URL without a
        # state: it generates one internally even though it returns str-only.
        _settings(monkeypatch)
        captured = {}

        def _build_url(*, state, scope=_DEFAULT_SCOPE):
            captured["state"] = state
            return f"https://github.com/login/oauth/authorize?state={state}"

        oauth = _stub_oauth()
        oauth.get_authorize_url = MagicMock(side_effect=_build_url)
        url = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url()

        assert isinstance(url, str)
        assert _state_from_url(url) == captured["state"] == "auto-state-token"
        oauth.generate_state.assert_called_once()

    def test_both_accessors_embed_identical_state(self):
        # For the same supplied state, ``get_authorize_url`` and
        # ``get_authorize_url_with_state`` embed the identical value -- they
        # share one code path.
        def _build_url(*, state, scope=_DEFAULT_SCOPE):
            return f"https://github.com/login/oauth/authorize?state={state}"

        oauth = _stub_oauth()
        oauth.get_authorize_url = MagicMock(side_effect=_build_url)
        adapter = GitHubAuthProvider(oauth_provider=oauth)

        url_str = adapter.get_authorize_url(state="shared-state")
        url_tuple, _ = adapter.get_authorize_url_with_state(state="shared-state")

        assert _state_from_url(url_str) == _state_from_url(url_tuple) == "shared-state"

    def test_get_authorize_url_passes_default_scope(self):
        # The adapter requests the GitHub scopes (read:user + user:email)
        # through the underlying provider.
        oauth = _stub_oauth()
        GitHubAuthProvider(oauth_provider=oauth).get_authorize_url_with_state(state="x")
        oauth.get_authorize_url.assert_called_once()
        assert oauth.get_authorize_url.call_args.kwargs["scope"] == _DEFAULT_SCOPE


# ===========================================================================
# Route: the authorize_provider coroutine is the exported callable
# ===========================================================================
class TestAuthorizeProviderCallable:
    """Sanity: the route handler is importable and is a coroutine function
    (so the framework actually awaits it). Guards against an accidental
    refactor that breaks the route registration."""

    def test_authorize_provider_is_async(self):
        import inspect

        assert inspect.iscoroutinefunction(authorize_provider)


# ===========================================================================
# Route: validate_authorize_url / validate_oauth_state defence-in-depth
# ===========================================================================
class TestAuthorizeUrlAndStateValidation:
    """The route must refuse to emit a non-``https://`` authorize URL (e.g. a
    ``javascript:`` or protocol-relative ``//evil`` payload) or a
    malformed/short/non-string ``state`` token, surfacing a generic 500 --
    never the bad value itself -- and logging a security warning. Covers the
    tuple return (``get_authorize_url_with_state``) and the plain-string return
    (``get_authorize_url``) paths."""

    @pytest.mark.asyncio
    async def test_valid_tuple_is_accepted(self, db_session):
        # A well-formed ``(https_url, urlsafe state >= 16 chars)`` tuple round
        # trips through to a 200 with both values surfaced unchanged.
        class ValidTupleProvider:
            name = "validtuple"

            def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
                return "https://idp.example.com/auth", "good-urlsafe-state-token"

        app = _boot_app_with_provider(db_session, ValidTupleProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/validtuple/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["authorize_url"] == "https://idp.example.com/auth"
        assert data["state"] == "good-urlsafe-state-token"
        assert resp.cookies.get("oauth_state_validtuple") == "good-urlsafe-state-token"

    @pytest.mark.asyncio
    async def test_tuple_with_none_elements_returns_500(self, db_session):
        # A provider returning ``(None, None)`` (e.g. an unconfigured client)
        # must never emit ``None`` to the victim's browser -- rejected.
        class NoneTupleProvider:
            name = "nonetuple"

            def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
                return (None, None)  # type: ignore[return-value]

        app = _boot_app_with_provider(db_session, NoneTupleProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/nonetuple/authorize")

        assert resp.status_code == 500
        assert "authorize URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_tuple_with_empty_state_returns_500(self, db_session):
        # An empty/missing state would defeat the CSRF protection on callback;
        # rejected even though the URL itself is valid https.
        class EmptyStateProvider:
            name = "emptystate"

            def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
                return ("https://idp.example.com/auth", "")

        app = _boot_app_with_provider(db_session, EmptyStateProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/emptystate/authorize")

        assert resp.status_code == 500
        assert "authorize URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_plain_string_javascript_url_returns_500(self, db_session):
        # A ``javascript:`` payload would execute script in the victim's
        # browser if bounced through the redirect -- must be rejected.
        class JavascriptUrlProvider:
            name = "jsurl"

            def get_authorize_url(self, state: str = "") -> str:
                return "javascript:alert(document.cookie)"

        app = _boot_app_with_provider(db_session, JavascriptUrlProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/jsurl/authorize")

        assert resp.status_code == 500
        assert "authorize URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_plain_string_protocol_relative_url_returns_500(self, db_session):
        # A protocol-relative ``//evil`` URL inherits the host page's scheme and
        # can leak the redirect to an attacker-controlled origin.
        class ProtocolRelativeProvider:
            name = "proprel"

            def get_authorize_url(self, state: str = "") -> str:
                return "//evil.example.com/auth"

        app = _boot_app_with_provider(db_session, ProtocolRelativeProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/proprel/authorize")

        assert resp.status_code == 500
        assert "authorize URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_valid_https_url_string_is_accepted(self, db_session):
        # A plain-string ``https://`` URL on the fallback path is accepted; the
        # route's minted state is surfaced and persisted in the session cookie.
        class HttpsStringProvider:
            name = "httpsstr"

            def get_authorize_url(self, state: str = "") -> str:
                return "https://idp.example.com/auth?state=" + state

        app = _boot_app_with_provider(db_session, HttpsStringProvider())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/httpsstr/authorize")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["authorize_url"].startswith("https://")
        assert resp.cookies.get("oauth_state_httpsstr") == data["state"]

    @pytest.mark.asyncio
    async def test_security_warning_logged_on_invalid_url(self, db_session):
        # The defence-in-depth guard must emit a structured security warning so
        # an attempted exploit is observable in the audit log.
        class JavascriptUrlProvider:
            name = "jsurl2"

            def get_authorize_url(self, state: str = "") -> str:
                return "javascript:alert(1)"

        app = _boot_app_with_provider(db_session, JavascriptUrlProvider())
        transport = ASGITransport(app=app)
        with pytest.MonkeyPatch().context() as mp:
            mock_logger = MagicMock()
            mp.setattr("engine.api.routes.auth.logger", mock_logger)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.get("/api/v1/auth/jsurl2/authorize")

        assert resp.status_code == 500
        mock_logger.warning.assert_called_once()
        assert "invalid_provider_url" in mock_logger.warning.call_args.args[0]


# ===========================================================================
# Helpers: validate_authorize_url / validate_oauth_state contract
# ===========================================================================
class TestAuthorizeValidatorsDirect:
    """Pin the contract of the validator helpers independently of the route
    wiring so the security boundary is explicit."""

    @pytest.mark.parametrize("bad", [None, 123, 4.5, object(), b"https://x", ""])
    def test_validate_authorize_url_rejects_non_string_or_empty(self, bad):
        assert validate_authorize_url(bad) is False

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "//evil.example.com",
            "http://insecure.example.com",
            "ftp://example.com",
            " https://idp/auth",  # leading space -- not a clean https:// prefix
        ],
    )
    def test_validate_authorize_url_rejects_wrong_scheme(self, url):
        assert validate_authorize_url(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://idp.example.com/auth",
            "https://github.com/login/oauth/authorize?state=x",
        ],
    )
    def test_validate_authorize_url_accepts_https(self, url):
        assert validate_authorize_url(url) is True

    @pytest.mark.parametrize("bad", [None, 123, 4.5, object(), "", "short", "a" * 15])
    def test_validate_oauth_state_rejects_non_string_or_too_short(self, bad):
        assert validate_oauth_state(bad) is False

    def test_validate_oauth_state_rejects_non_urlsafe_characters(self):
        # Spaces, slashes, query separators and punctuation are NOT in the
        # urlsafe alphabet and would corrupt the cookie store / redirect URL.
        assert validate_oauth_state("contains space here!!") is False
        assert validate_oauth_state("a" * 15 + "/") is False

    def test_validate_oauth_state_accepts_urlsafe_tokens(self):
        # ``secrets.token_urlsafe`` output is the canonical acceptable value.
        assert validate_oauth_state(secrets.token_urlsafe(32)) is True
        assert validate_oauth_state("good-urlsafe-state-token") is True
        # Exactly 16 urlsafe chars is the floor and must pass.
        assert validate_oauth_state("a" * 16) is True
