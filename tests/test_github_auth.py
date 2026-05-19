"""Comprehensive tests for engine/api/auth/github_oauth.py.

Covers:
  - name property
  - authenticate (missing params, token exchange failure, userinfo failure,
    happy path new user, existing user, email conflict, disabled user,
    incomplete profile, name/email fallbacks)
  - get_authorize_url (with/without state)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.config import Settings


class _FakeHttpxResponse:
    def __init__(self, json_data=None, raise_error=None):
        self._json_data = json_data
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        if self._get_responses:
            return self._get_responses.pop(0)
        return _FakeHttpxResponse(json_data={})

    async def post(self, url, **kwargs):
        if self._post_responses:
            return self._post_responses.pop(0)
        return _FakeHttpxResponse(json_data={})


@pytest.fixture
def github_provider():
    return GitHubAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        github_client_id="test-github-client-id",
        github_client_secret="test-github-client-secret",
        github_redirect_uri="https://app.example.com/auth/github/callback",
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


GITHUB_PROFILE = {
    "id": 12345678,
    "login": "testuser",
    "email": "testuser@example.com",
    "name": "Test User",
}


def _build_mock_client(profile=None, token_error=None, profile_error=None):
    profile = profile or GITHUB_PROFILE
    token_resp = _FakeHttpxResponse(
        json_data={"access_token": "github-at", "token_type": "bearer"},
        raise_error=token_error,
    )
    profile_resp = _FakeHttpxResponse(
        json_data=profile,
        raise_error=profile_error,
    )
    return _FakeAsyncClient(
        get_responses=[profile_resp],
        post_responses=[token_resp],
    )


class TestGitHubNameProperty:
    def test_name_returns_github(self, github_provider):
        assert github_provider.name == "github"


class TestGitHubAuthenticate:
    async def test_authenticate_missing_code(self, github_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await github_provider.authenticate(db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_db(self, github_provider, mock_settings):
        result = await github_provider.authenticate(code="abc")
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_both(self, github_provider, mock_settings):
        result = await github_provider.authenticate()
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_token_exchange_fails(self, github_provider, mock_settings):
        fake_client = _build_mock_client(
            token_error=Exception("Token exchange failed"),
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "GitHub authentication failed" in result.error

    async def test_authenticate_profile_fetch_fails(self, github_provider, mock_settings):
        fake_client = _build_mock_client(
            profile_error=Exception("Profile fetch failed"),
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "GitHub authentication failed" in result.error

    async def test_authenticate_happy_path_new_user(self, github_provider, mock_settings):
        fake_client = _build_mock_client()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "testuser@example.com"
        assert result.user_info.provider == "github"
        assert result.user_info.external_id == "12345678"
        assert result.user_info.display_name == "Test User"
        assert result.user_info.roles == ["user"]
        mock_db.add.assert_called_once()

    async def test_authenticate_existing_github_user(self, github_provider, mock_settings):
        from engine.db.models import User

        fake_client = _build_mock_client()

        existing_user = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="user",
            auth_provider="github",
            external_id="12345678",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "testuser@example.com"
        mock_db.add.assert_not_called()

    async def test_authenticate_email_conflict_different_provider(
        self, github_provider, mock_settings
    ):
        from engine.db.models import User

        fake_client = _build_mock_client()

        conflict_user = User(
            email="testuser@example.com",
            display_name="Conflict User",
            auth_provider="local",
        )
        mock_db = AsyncMock(spec=AsyncSession)

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalar_one_or_none.return_value = None
            else:
                r.scalar_one_or_none.return_value = conflict_user
            return r

        mock_db.execute = mock_execute

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "different provider" in result.error

    async def test_authenticate_disabled_user(self, github_provider, mock_settings):
        from engine.db.models import User

        fake_client = _build_mock_client()

        disabled_user = User(
            email="testuser@example.com",
            display_name="Disabled User",
            is_active=False,
            auth_provider="github",
            external_id="12345678",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "disabled" in result.error

    async def test_authenticate_incomplete_profile_missing_id(
        self, github_provider, mock_settings
    ):
        profile = {"id": None, "login": "testuser", "email": "t@e.com", "name": "Test"}
        fake_client = _build_mock_client(profile=profile)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_authenticate_fallback_email_when_null(
        self, github_provider, mock_settings
    ):
        profile = {"id": 99999, "login": "ghuser", "email": None, "name": "GH User"}
        fake_client = _build_mock_client(profile=profile)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "ghuser@github"

    async def test_authenticate_fallback_name_when_null(
        self, github_provider, mock_settings
    ):
        profile = {
            "id": 88888,
            "login": "loginuser",
            "email": "loginuser@example.com",
            "name": None,
        }
        fake_client = _build_mock_client(profile=profile)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.display_name == "loginuser"

    async def test_authenticate_fallback_name_when_null_login_missing(
        self, github_provider, mock_settings
    ):
        profile = {
            "id": 77777,
            "login": None,
            "email": "nobody@example.com",
            "name": None,
        }
        fake_client = _build_mock_client(profile=profile)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.display_name == "GitHub User"

    async def test_authenticate_token_exchange_sends_correct_params(
        self, github_provider, mock_settings
    ):
        posted_data = {}
        posted_headers = {}

        class CapturingClient(_FakeAsyncClient):
            async def post(self, url, **kwargs):
                posted_data.update(kwargs.get("data", {}))
                posted_headers.update(kwargs.get("headers", {}))
                return _FakeHttpxResponse(
                    json_data={"access_token": "github-at"},
                )

        profile_resp = _FakeHttpxResponse(json_data=GITHUB_PROFILE)
        fake_client = CapturingClient(get_responses=[profile_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await github_provider.authenticate(code="my-auth-code", db=mock_db)

        assert posted_data["code"] == "my-auth-code"
        assert posted_data["client_id"] == "test-github-client-id"
        assert posted_data["client_secret"] == "test-github-client-secret"
        assert posted_data["redirect_uri"] == "https://app.example.com/auth/github/callback"
        assert posted_headers.get("Accept") == "application/json"

    async def test_authenticate_sends_authorization_header_for_profile(
        self, github_provider, mock_settings
    ):
        get_kwargs_captured = {}

        class CapturingClient(_FakeAsyncClient):
            async def get(self, url, **kwargs):
                get_kwargs_captured.update(kwargs)
                return _FakeHttpxResponse(json_data=GITHUB_PROFILE)

        token_resp = _FakeHttpxResponse(
            json_data={"access_token": "my-github-token"},
        )
        fake_client = CapturingClient(post_responses=[token_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await github_provider.authenticate(code="auth-code", db=mock_db)

        assert (
            get_kwargs_captured.get("headers", {}).get("Authorization")
            == "Bearer my-github-token"
        )


class TestGitHubAuthorizeUrl:
    async def test_get_authorize_url(self, github_provider, mock_settings):
        url = await github_provider.get_authorize_url()
        assert "github.com/login/oauth/authorize" in url
        assert "client_id=test-github-client-id" in url
        assert "redirect_uri=" in url
        assert "scope=user:email" in url
        assert "state=" not in url

    async def test_get_authorize_url_with_state(self, github_provider, mock_settings):
        url = await github_provider.get_authorize_url(state="random-state-456")
        assert "state=random-state-456" in url


class TestGitHubRoleMapping:
    def test_map_roles_admin_wins(self, github_provider):
        assert github_provider.map_roles(["user", "admin", "developer"]) == "admin"

    def test_map_roles_developer_wins_over_user(self, github_provider):
        assert github_provider.map_roles(["user", "developer"]) == "developer"

    def test_map_roles_user_default(self, github_provider):
        assert github_provider.map_roles(["user"]) == "user"

    def test_map_roles_unknown_role(self, github_provider):
        assert github_provider.map_roles(["unknown_role"]) == "user"

    def test_map_roles_empty_list(self, github_provider):
        assert github_provider.map_roles([]) == "user"
