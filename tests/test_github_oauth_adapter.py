"""Unit tests for ``engine/api/auth/github_oauth.py`` -- the registry adapter
that wires the GitHub OAuth2 provider into the FastAPI auth router.

The adapter delegates the networked OAuth2 steps (token exchange, profile
fetch) to :class:`engine.auth.github.GitHubOAuthProvider`. To keep these tests
hermetic we inject a stub provider whose ``exchange_code`` /
``validate_access_token`` are :class:`~unittest.mock.AsyncMock` instances,
then assert on the adapter's *domain* behavior:

* validating inputs,
* translating OAuth2 failures into :class:`AuthResult` values,
* looking up / creating Nexus users,
* guarding against email re-use and disabled accounts,
* mapping the validated profile onto :class:`UserInfo`.

The DB layer is mocked with ``AsyncMock(spec=AsyncSession)`` following the
same pattern as ``tests/test_oidc_auth.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import AuthResult, UserInfo
from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.auth.base import TokenSet
from engine.auth.github import (
    GitHubOAuthError,
    GitHubUserInfo,
    InvalidTokenError,
    TokenExchangeError,
)
from engine.config import Settings
from engine.db.models import User

# --- Fixtures ---------------------------------------------------------------
_TOKEN_SET = TokenSet(access_token="gho_token", token_type="bearer")
_PROFILE = GitHubUserInfo(
    id="12345",
    login="octocat",
    email="octo@example.com",
    name="The Octocat",
)


def _make_oauth_provider(
    *,
    exchange=None,
    validate=None,
) -> MagicMock:
    """Build a stub :class:`GitHubOAuthProvider`.

    By default the token exchange + profile fetch succeed; pass ``exchange`` /
    ``validate`` to make either side raise (an exception instance or class) or
    return a custom value.
    """
    provider = MagicMock()
    provider.name = "github"

    if exchange is None:
        provider.exchange_code = AsyncMock(return_value=_TOKEN_SET)
    elif isinstance(exchange, BaseException) or (
        isinstance(exchange, type) and issubclass(exchange, BaseException)
    ):
        exc = exchange if isinstance(exchange, BaseException) else exchange("boom")
        provider.exchange_code = AsyncMock(side_effect=exc)
    else:
        provider.exchange_code = AsyncMock(return_value=exchange)

    if validate is None:
        provider.validate_access_token = AsyncMock(return_value=_PROFILE)
    elif isinstance(validate, BaseException) or (
        isinstance(validate, type) and issubclass(validate, BaseException)
    ):
        exc = validate if isinstance(validate, BaseException) else validate("boom")
        provider.validate_access_token = AsyncMock(side_effect=exc)
    else:
        provider.validate_access_token = AsyncMock(return_value=validate)

    provider.get_authorize_url = MagicMock(return_value="https://github.com/login/oauth/authorize?state=s")
    return provider


def _mock_db(scalar_results):
    """Async session whose consecutive ``execute().scalar_one_or_none()`` calls
    return the supplied list of values (in order)."""
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    results = list(scalar_results)
    it = iter(results)

    async def mock_execute(_stmt):
        r = MagicMock()
        r.scalar_one_or_none.return_value = next(it, None)
        return r

    db.execute = mock_execute
    return db


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        github_client_id="gh-id",
        github_client_secret="gh-secret",
        github_redirect_uri="https://app.example.com/callback",
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


# ===========================================================================
# Identity / authorize URL
# ===========================================================================
class TestAdapterIdentity:
    def test_name_is_github(self):
        assert GitHubAuthProvider(oauth_provider=_make_oauth_provider()).name == "github"

    def test_get_authorize_url_delegates_to_provider(self):
        oauth = _make_oauth_provider()
        url, state = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url(state="abc")
        oauth.get_authorize_url.assert_called_once()
        kwargs = oauth.get_authorize_url.call_args.kwargs
        assert kwargs["state"] == "abc"
        assert url.startswith("https://github.com/login/oauth/authorize")
        # The state is returned alongside the URL so the caller can persist
        # and validate it on the callback.
        assert state == "abc"

    def test_get_authorize_url_with_state_round_trips_state(self):
        # The canonical, typed ``(url, state)`` accessor (spec point 3) returns
        # the state alongside the URL so the route can persist/validate it.
        oauth = _make_oauth_provider()
        url, state = GitHubAuthProvider(oauth_provider=oauth).get_authorize_url_with_state(
            state="xyz"
        )
        oauth.get_authorize_url.assert_called_once()
        assert oauth.get_authorize_url.call_args.kwargs["state"] == "xyz"
        assert url.startswith("https://github.com/login/oauth/authorize")
        assert state == "xyz"

    def test_get_authorize_url_without_state_auto_generates(self, mock_settings):
        # No state supplied and no injected provider -> the adapter lazily
        # builds one from settings AND auto-generates a CSRF ``state`` token,
        # so the URL is never produced without CSRF protection. Assertions use
        # ``parse_qs`` so they compare *decoded* values rather than the raw
        # percent-encoded query string.
        adapter = GitHubAuthProvider()
        url, state = adapter.get_authorize_url()
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "github.com"
        assert parsed.path == "/login/oauth/authorize"
        params = parse_qs(parsed.query)
        assert params["client_id"] == ["gh-id"]
        assert params["redirect_uri"] == ["https://app.example.com/callback"]
        assert "read:user" in params["scope"][0]
        assert "user:email" in params["scope"][0]
        # state is always present and non-empty (auto-generated).
        assert params["state"]
        assert params["state"][0]
        # The auto-generated state is returned alongside the URL so the
        # caller can persist and validate it on the callback -- otherwise the
        # CSRF protection would be unenforceable.
        assert state == params["state"][0]

    def test_get_oauth_lazily_built_from_settings(self, mock_settings):
        adapter = GitHubAuthProvider()
        oauth = adapter._get_oauth()
        assert oauth.client_id == "gh-id"
        assert oauth.client_secret == "gh-secret"
        # Repeated access reuses the cached instance.
        assert adapter._get_oauth() is oauth


# ===========================================================================
# authenticate: input validation
# ===========================================================================
class TestAuthenticateInputs:
    async def test_missing_code(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        result = await adapter.authenticate(db=AsyncMock(spec=AsyncSession))
        assert result.success is False
        assert "code" in result.error

    async def test_missing_db(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        result = await adapter.authenticate(code="abc")
        assert result.success is False
        assert "db session" in result.error

    async def test_missing_both(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        result = await adapter.authenticate()
        assert result.success is False


# ===========================================================================
# authenticate: OAuth2 failure translation
# ===========================================================================
class TestAuthenticateFailures:
    async def test_token_exchange_failure(self):
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(exchange=TokenExchangeError("nope"))
        )
        result = await adapter.authenticate(code="abc", db=_mock_db([None]))
        assert result.success is False
        assert "token exchange" in result.error.lower()

    async def test_invalid_token_failure(self):
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(validate=InvalidTokenError("bad"))
        )
        result = await adapter.authenticate(code="abc", db=_mock_db([None]))
        assert result.success is False
        assert "invalid token" in result.error.lower()

    async def test_generic_oauth_error(self):
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(validate=GitHubOAuthError("boom"))
        )
        result = await adapter.authenticate(code="abc", db=_mock_db([None]))
        assert result.success is False
        assert result.error == "GitHub authentication failed"

    async def test_incomplete_profile_returns_failure(self):
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(
                validate=GitHubUserInfo(id="", login="", email="", name="")
            )
        )
        result = await adapter.authenticate(code="abc", db=_mock_db([None]))
        assert result.success is False
        assert "Incomplete" in result.error


# ===========================================================================
# authenticate: happy paths
# ===========================================================================
class TestAuthenticateHappyPath:
    async def test_new_user_is_created(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])  # no provider match, no email conflict

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.provider == "github"
        assert result.user_info.external_id == "12345"
        assert result.user_info.email == "octo@example.com"
        assert result.user_info.display_name == "The Octocat"
        assert result.user_info.roles == ["user"]
        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        db.refresh.assert_awaited_once()

    async def test_existing_user_no_creation(self):
        existing = User(
            email="octo@example.com",
            display_name="Existing Octocat",
            is_active=True,
            role="user",
            auth_provider="github",
            external_id="12345",
        )
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([existing])  # found on first lookup

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is True
        assert result.user_info.email == "octo@example.com"
        assert result.user_info.display_name == "Existing Octocat"
        db.add.assert_not_called()
        db.flush.assert_not_awaited()


# ===========================================================================
# authenticate: domain guard rails
# ===========================================================================
class TestAuthenticateGuardRails:
    async def test_email_conflict_different_provider(self):
        conflict = User(
            email="octo@example.com",
            display_name="Local Octo",
            auth_provider="local",
        )
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, conflict])  # no provider match, email taken

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is False
        assert "different provider" in result.error
        db.add.assert_not_called()

    async def test_disabled_user_rejected(self):
        disabled = User(
            email="octo@example.com",
            display_name="Octo",
            is_active=False,
            auth_provider="github",
            external_id="12345",
        )
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([disabled])

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is False
        assert "disabled" in result.error

    async def test_returned_user_info_is_userinfo_instance(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])

        result = await adapter.authenticate(code="abc", db=db)

        assert isinstance(result, AuthResult)
        assert isinstance(result.user_info, UserInfo)


# ===========================================================================
# authenticate: CSRF state validation (defense in depth)
# ===========================================================================
class TestAuthenticateStateValidation:
    """The adapter offers a constant-time ``state`` check via
    ``hmac.compare_digest`` for callers that use it as a standalone API. The
    HTTP route performs its own cookie-based check, so when no
    ``expected_state`` is supplied validation is skipped (backward compatible).
    """

    async def test_matching_state_is_accepted(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])

        result = await adapter.authenticate(
            code="abc", db=db, state="csrf-token", expected_state="csrf-token"
        )

        assert result.success is True
        assert result.user_info is not None

    async def test_mismatched_state_is_rejected_before_profile_fetch(self):
        # A mismatch must short-circuit *before* the networked profile fetch,
        # so the OAuth provider is never contacted.
        oauth = _make_oauth_provider()
        adapter = GitHubAuthProvider(oauth_provider=oauth)
        db = _mock_db([None, None])

        result = await adapter.authenticate(
            code="abc", db=db, state="attacker-state", expected_state="issued-state"
        )

        assert result.success is False
        assert "CSRF" in result.error or "state" in result.error.lower()
        oauth.exchange_code.assert_not_awaited()
        oauth.validate_access_token.assert_not_awaited()
        db.add.assert_not_called()

    async def test_missing_received_state_with_expected_is_rejected(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])

        result = await adapter.authenticate(code="abc", db=db, expected_state="issued-state")

        assert result.success is False
        assert "CSRF" in result.error or "state" in result.error.lower()

    async def test_empty_received_state_with_expected_is_rejected(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])

        result = await adapter.authenticate(
            code="abc", db=db, state="", expected_state="issued-state"
        )

        assert result.success is False

    async def test_non_string_state_is_rejected(self):
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])

        result = await adapter.authenticate(
            code="abc", db=db, state=None, expected_state="issued-state"
        )

        assert result.success is False

    async def test_no_expected_state_skips_validation(self):
        # When expected_state is not supplied (the HTTP route path, which
        # validates via a signed cookie itself), the adapter does not enforce
        # state and the flow proceeds normally.
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())
        db = _mock_db([None, None])

        result = await adapter.authenticate(code="abc", db=db, state="anything")

        assert result.success is True

    async def test_validation_runs_before_input_error_is_returned(self):
        # Input validation (code/db presence) still takes precedence over the
        # state check: a missing code is reported as the code error, not CSRF.
        adapter = GitHubAuthProvider(oauth_provider=_make_oauth_provider())

        result = await adapter.authenticate(
            db=_mock_db([]), state="x", expected_state="y"
        )

        assert result.success is False
        assert "code" in result.error


# ===========================================================================
# authenticate: NULL / blank email guard on new-user creation
# ===========================================================================
class TestAuthenticateNullEmailGuard:
    """The new-user creation path must never persist a ``User`` with a NULL or
    blank email (the column is UNIQUE NOT NULL). The provider normally
    synthesizes a noreply address, but the adapter defends in depth.
    """

    async def test_blank_email_on_new_user_is_rejected(self):
        profile = GitHubUserInfo(
            id="12345", login="octocat", email="", name="The Octocat"
        )
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(validate=profile)
        )
        db = _mock_db([None, None])  # no provider match, no email conflict

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is False
        assert "email" in result.error.lower()
        # No user row is ever persisted.
        db.add.assert_not_called()
        db.flush.assert_not_awaited()

    async def test_none_email_on_new_user_is_rejected(self):
        # ``info.email`` is typed ``str`` but defence in depth: an explicit
        # ``None`` must be rejected *before* the uniqueness query, otherwise
        # ``User.email == None`` degrades to ``User.email IS NULL`` and would
        # match every NULL row.
        profile = GitHubUserInfo(
            id="12345", login="octocat", email=None, name="The Octocat"  # type: ignore[arg-type]
        )
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(validate=profile)
        )
        db = _mock_db([None, None])  # no provider match, no email conflict

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is False
        assert "email" in result.error.lower()
        # The uniqueness query is never executed, so only the first lookup
        # (provider/external_id) ran.
        db.add.assert_not_called()
        db.flush.assert_not_awaited()

    async def test_existing_user_with_blank_email_is_not_blocked_by_guard(self):
        # The guard only applies to the *new-user* branch. An existing user
        # (already linked to GitHub) authenticates even if the freshly-fetched
        # profile somehow lacks an email -- we trust the stored record.
        existing = User(
            email="octo@example.com",
            display_name="Octo",
            is_active=True,
            role="user",
            auth_provider="github",
            external_id="12345",
        )
        profile = GitHubUserInfo(id="12345", login="octocat", email="", name="Octo")
        adapter = GitHubAuthProvider(
            oauth_provider=_make_oauth_provider(validate=profile)
        )
        db = _mock_db([existing])  # found on first lookup

        result = await adapter.authenticate(code="abc", db=db)

        assert result.success is True
        assert result.user_info.email == "octo@example.com"
        db.add.assert_not_called()
