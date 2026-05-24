"""Comprehensive tests for engine/api/auth/google.py.

Covers:
  - name property
  - authenticate (missing params, token exchange failure, userinfo failure,
    happy path new user, existing user, email conflict, disabled user,
    incomplete profile)
  - get_authorize_url (with/without state)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.google import GoogleAuthProvider
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
def google_provider():
    return GoogleAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        google_client_id="test-google-client-id",
        google_client_secret="test-google-client-secret",
        google_redirect_uri="https://app.example.com/auth/google/callback",
    )
    monkeypatch.setattr("engine.api.auth.google.settings", s)
    return s


GOOGLE_USERINFO = {
    "sub": "google-user-123",
    "email": "newuser@gmail.com",
    "name": "New User",
    "picture": "https://lh3.googleusercontent.com/example",
}


def _build_mock_client(userinfo=None, token_error=None, userinfo_error=None):
    userinfo = userinfo or GOOGLE_USERINFO
    token_resp = _FakeHttpxResponse(
        json_data={"access_token": "google-at", "token_type": "Bearer"},
        raise_error=token_error,
    )
    userinfo_resp = _FakeHttpxResponse(
        json_data=userinfo,
        raise_error=userinfo_error,
    )
    return _FakeAsyncClient(
        get_responses=[userinfo_resp],
        post_responses=[token_resp],
    )


async def _set_is_active(user):
    user.is_active = True


class TestGoogleNameProperty:
    def test_name_returns_google(self, google_provider):
        assert google_provider.name == "google"


class TestGoogleAuthenticate:
    async def test_authenticate_missing_code(self, google_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await google_provider.authenticate(db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_db(self, google_provider, mock_settings):
        result = await google_provider.authenticate(code="abc")
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_both(self, google_provider, mock_settings):
        result = await google_provider.authenticate()
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_token_exchange_fails(self, google_provider, mock_settings):
        fake_client = _build_mock_client(
            token_error=Exception("Token exchange failed"),
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Google authentication failed" in result.error

    async def test_authenticate_userinfo_fails(self, google_provider, mock_settings):
        fake_client = _build_mock_client(
            userinfo_error=Exception("Userinfo fetch failed"),
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Google authentication failed" in result.error

    async def test_authenticate_happy_path_new_user(self, google_provider, mock_settings):
        fake_client = _build_mock_client()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = _set_is_active

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "newuser@gmail.com"
        assert result.user_info.provider == "google"
        assert result.user_info.external_id == "google-user-123"
        assert result.user_info.display_name == "New User"
        assert result.user_info.roles == ["user"]
        mock_db.add.assert_called_once()

    async def test_authenticate_existing_google_user(self, google_provider, mock_settings):
        from engine.db.models import User

        fake_client = _build_mock_client()

        existing_user = User(
            email="newuser@gmail.com",
            display_name="New User",
            is_active=True,
            role="user",
            auth_provider="google",
            external_id="google-user-123",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "newuser@gmail.com"
        mock_db.add.assert_not_called()

    async def test_authenticate_email_conflict_different_provider(
        self, google_provider, mock_settings
    ):
        from engine.db.models import User

        fake_client = _build_mock_client()

        conflict_user = User(
            email="newuser@gmail.com",
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
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "different provider" in result.error

    async def test_authenticate_disabled_user(self, google_provider, mock_settings):
        from engine.db.models import User

        fake_client = _build_mock_client()

        disabled_user = User(
            email="newuser@gmail.com",
            display_name="Disabled User",
            is_active=False,
            auth_provider="google",
            external_id="google-user-123",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "disabled" in result.error

    async def test_authenticate_incomplete_profile_missing_sub(
        self, google_provider, mock_settings
    ):
        fake_client = _build_mock_client(
            userinfo={"sub": "", "email": "nosub@gmail.com", "name": "No Sub"},
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_authenticate_incomplete_profile_missing_email(
        self, google_provider, mock_settings
    ):
        fake_client = _build_mock_client(
            userinfo={"sub": "google-123", "email": "", "name": "No Email"},
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_authenticate_name_fallback_to_email_prefix(
        self, google_provider, mock_settings
    ):
        userinfo = {"sub": "google-no-name", "email": "john.doe@gmail.com"}
        fake_client = _build_mock_client(userinfo=userinfo)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = _set_is_active

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.display_name == "john.doe"

    async def test_authenticate_token_exchange_sends_correct_params(
        self, google_provider, mock_settings
    ):
        posted_data = {}

        class CapturingClient(_FakeAsyncClient):
            async def post(self, url, **kwargs):
                posted_data.update(kwargs.get("data", {}))
                return _FakeHttpxResponse(
                    json_data={"access_token": "google-at"},
                )

        userinfo_resp = _FakeHttpxResponse(json_data=GOOGLE_USERINFO)
        fake_client = CapturingClient(get_responses=[userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = _set_is_active

        with patch("httpx.AsyncClient", return_value=fake_client):
            await google_provider.authenticate(code="my-auth-code", db=mock_db)

        assert posted_data["code"] == "my-auth-code"
        assert posted_data["client_id"] == "test-google-client-id"
        assert posted_data["client_secret"] == "test-google-client-secret"
        assert posted_data["redirect_uri"] == "https://app.example.com/auth/google/callback"
        assert posted_data["grant_type"] == "authorization_code"

    async def test_authenticate_sends_authorization_header_for_userinfo(
        self, google_provider, mock_settings
    ):
        get_kwargs_captured = {}

        class CapturingClient(_FakeAsyncClient):
            async def get(self, url, **kwargs):
                get_kwargs_captured.update(kwargs)
                return _FakeHttpxResponse(json_data=GOOGLE_USERINFO)

        token_resp = _FakeHttpxResponse(
            json_data={"access_token": "my-access-token"},
        )
        fake_client = CapturingClient(post_responses=[token_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = _set_is_active

        with patch("httpx.AsyncClient", return_value=fake_client):
            await google_provider.authenticate(code="auth-code", db=mock_db)

        assert get_kwargs_captured.get("headers", {}).get("Authorization") == "Bearer my-access-token"


class TestGoogleAuthorizeUrl:
    async def test_get_authorize_url(self, google_provider, mock_settings):
        url = await google_provider.get_authorize_url()
        assert "accounts.google.com/o/oauth2/v2/auth" in url
        assert "client_id=test-google-client-id" in url
        assert "redirect_uri=" in url
        assert "response_type=code" in url
        assert "scope=openid email profile" in url
        assert "state=" not in url

    async def test_get_authorize_url_with_state(self, google_provider, mock_settings):
        url = await google_provider.get_authorize_url(state="random-state-123")
        assert "state=random-state-123" in url


class TestGoogleRoleMapping:
    def test_map_roles_admin_wins(self, google_provider):
        assert google_provider.map_roles(["user", "admin", "developer"]) == "admin"

    def test_map_roles_developer_wins_over_user(self, google_provider):
        assert google_provider.map_roles(["user", "developer"]) == "developer"

    def test_map_roles_user_default(self, google_provider):
        assert google_provider.map_roles(["user"]) == "user"

    def test_map_roles_unknown_role(self, google_provider):
        assert google_provider.map_roles(["unknown_role"]) == "user"

    def test_map_roles_empty_list(self, google_provider):
        assert google_provider.map_roles([]) == "user"

    def test_map_roles_case_insensitive(self, google_provider):
        assert google_provider.map_roles(["ADMIN"]) == "admin"
        assert google_provider.map_roles(["  Admin  "]) == "admin"
