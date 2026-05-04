from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.api.auth.base import AuthResult
from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.db.models import User


@pytest.fixture
def provider():
    return GitHubAuthProvider()


class TestGitHubAuthProviderName:
    def test_name(self, provider):
        assert provider.name == "github"


class TestGitHubGetAuthorizeUrl:
    def test_url_contains_client_id(self, provider):
        url = provider.get_authorize_url()
        assert "client_id=" in url
        assert "github.com/login/oauth/authorize" in url

    def test_url_contains_state(self, provider):
        url = provider.get_authorize_url(state="abc123")
        assert "state=abc123" in url

    def test_url_without_state(self, provider):
        url = provider.get_authorize_url()
        assert "state=" not in url


class TestGitHubAuthenticate:
    async def test_missing_code_returns_error(self, provider, db_session):
        result = await provider.authenticate(db=db_session)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_missing_db_returns_error(self, provider):
        result = await provider.authenticate(code="abc")
        assert result.success is False

    async def test_httpx_error_returns_failure(self, provider, db_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("network error")
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await provider.authenticate(code="valid-code", db=db_session)
        assert result.success is False
        assert "GitHub authentication failed" in result.error

    async def test_successful_auth_creates_user(self, provider, db_session):
        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {"access_token": "gh-token"}
        mock_token_resp.raise_for_status = MagicMock()

        profile = {"id": 12345, "login": "octocat", "email": "octo@example.com", "name": "Octo Cat"}
        mock_user_resp = MagicMock()
        mock_user_resp.json.return_value = profile
        mock_user_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token_resp
        mock_client.get.return_value = mock_user_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await provider.authenticate(code="valid-code", db=db_session)

        assert result.success is True
        assert result.user_info.email == "octo@example.com"
        assert result.user_info.provider == "github"
        assert result.user_info.external_id == "12345"

    async def test_existing_user_authenticates(self, provider, db_session):
        user = User(
            email="octo@example.com",
            hashed_password=None,
            display_name="Octo Cat",
            role="user",
            auth_provider="github",
            external_id="12345",
        )
        db_session.add(user)
        await db_session.flush()

        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {"access_token": "gh-token"}
        mock_token_resp.raise_for_status = MagicMock()

        profile = {"id": 12345, "login": "octocat", "email": "octo@example.com", "name": "Octo Cat"}
        mock_user_resp = MagicMock()
        mock_user_resp.json.return_value = profile
        mock_user_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token_resp
        mock_client.get.return_value = mock_user_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await provider.authenticate(code="valid-code", db=db_session)

        assert result.success is True
        assert result.user_info.external_id == "12345"

    async def test_disabled_user_returns_error(self, provider, db_session):
        user = User(
            email="disabled@example.com",
            hashed_password=None,
            display_name="Disabled",
            role="user",
            is_active=False,
            auth_provider="github",
            external_id="99999",
        )
        db_session.add(user)
        await db_session.flush()

        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {"access_token": "gh-token"}
        mock_token_resp.raise_for_status = MagicMock()

        profile = {"id": 99999, "login": "disabled_user", "email": "disabled@example.com", "name": "Disabled"}
        mock_user_resp = MagicMock()
        mock_user_resp.json.return_value = profile
        mock_user_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token_resp
        mock_client.get.return_value = mock_user_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await provider.authenticate(code="valid-code", db=db_session)

        assert result.success is False
        assert "disabled" in result.error.lower()

    async def test_duplicate_email_returns_error(self, provider, db_session):
        user = User(
            email="taken@example.com",
            hashed_password="hash",
            display_name="Existing",
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {"access_token": "gh-token"}
        mock_token_resp.raise_for_status = MagicMock()

        profile = {"id": 55555, "login": "newuser", "email": "taken@example.com", "name": "New User"}
        mock_user_resp = MagicMock()
        mock_user_resp.json.return_value = profile
        mock_user_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token_resp
        mock_client.get.return_value = mock_user_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await provider.authenticate(code="valid-code", db=db_session)

        assert result.success is False
        assert "different provider" in result.error

    async def test_incomplete_profile_returns_error(self, provider, db_session):
        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {"access_token": "gh-token"}
        mock_token_resp.raise_for_status = MagicMock()

        profile = {"login": "noid"}
        mock_user_resp = MagicMock()
        mock_user_resp.json.return_value = profile
        mock_user_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token_resp
        mock_client.get.return_value = mock_user_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await provider.authenticate(code="valid-code", db=db_session)

        assert result.success is False
        assert "Incomplete" in result.error
