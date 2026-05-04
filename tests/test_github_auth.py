from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.github_oauth import GitHubAuthProvider
from engine.config import Settings


@pytest.fixture
def github_provider():
    return GitHubAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        github_client_id="gh-client-id",
        github_client_secret="gh-client-secret",
        github_redirect_uri="https://app.example.com/github/callback",
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


class _FakeResponse:
    def __init__(self, json_data=None, raise_error=None):
        self._json_data = json_data
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, responses=None):
        self._responses = list(responses or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        return self._responses.pop(0) if self._responses else _FakeResponse(json_data={})

    async def get(self, url, **kwargs):
        return self._responses.pop(0) if self._responses else _FakeResponse(json_data={})


async def _simulate_db_refresh(obj):
    if hasattr(obj, "is_active") and obj.is_active is None:
        obj.is_active = True
    if hasattr(obj, "id") and obj.id is None:
        obj.id = uuid.uuid4()


def _make_success_client():
    token_resp = _FakeResponse(json_data={"access_token": "at-gh"})
    userinfo_resp = _FakeResponse(
        json_data={
            "id": 42,
            "login": "ghuser",
            "email": "user@github.com",
            "name": "GH User",
        }
    )
    return _FakeClient(responses=[token_resp, userinfo_resp])


class TestGitHubNameProperty:
    def test_name(self, github_provider):
        assert github_provider.name == "github"


class TestGitHubAuthenticate:
    async def test_missing_code(self, github_provider, mock_settings):
        result = await github_provider.authenticate(db=AsyncMock(spec=AsyncSession))
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_missing_db(self, github_provider, mock_settings):
        result = await github_provider.authenticate(code="abc")
        assert result.success is False

    async def test_token_exchange_fails(self, github_provider, mock_settings):
        token_resp = _FakeResponse(
            raise_error=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
        fake_client = _FakeClient(responses=[token_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(
                code="c", db=AsyncMock(spec=AsyncSession)
            )

        assert result.success is False
        assert "GitHub authentication failed" in result.error

    async def test_incomplete_profile_no_id(self, github_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(json_data={"login": "x"})
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_new_user_created(self, github_provider, mock_settings):
        fake_client = _make_success_client()
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=_simulate_db_refresh)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="c", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "user@github.com"
        assert result.user_info.provider == "github"
        assert result.user_info.external_id == "42"
        assert result.user_info.display_name == "GH User"
        mock_db.add.assert_called_once()

    async def test_existing_github_user(self, github_provider, mock_settings):
        from engine.db.models import User

        fake_client = _make_success_client()
        existing = User(
            email="user@github.com",
            display_name="GH User",
            is_active=True,
            auth_provider="github",
            external_id="42",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="c", db=mock_db)

        assert result.success is True
        mock_db.add.assert_not_called()

    async def test_email_conflict(self, github_provider, mock_settings):
        from engine.db.models import User

        fake_client = _make_success_client()
        conflict = User(email="user@github.com", auth_provider="local")

        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None if call_count == 1 else conflict
            return r

        mock_db.execute = mock_execute

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "different provider" in result.error

    async def test_disabled_user(self, github_provider, mock_settings):
        from engine.db.models import User

        fake_client = _make_success_client()
        disabled = User(
            email="user@github.com",
            is_active=False,
            auth_provider="github",
            external_id="42",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await github_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "disabled" in result.error

    async def test_email_fallback_to_login(self, github_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(
            json_data={"id": 99, "login": "ghlogin", "email": None, "name": "N"}
        )
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=_simulate_db_refresh)

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            await github_provider.authenticate(code="c", db=mock_db)

        assert created_users[0].email == "ghlogin@github"

    async def test_name_fallback_to_login(self, github_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(
            json_data={"id": 100, "login": "loginuser", "email": "l@x.com", "name": None}
        )
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=_simulate_db_refresh)

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            await github_provider.authenticate(code="c", db=mock_db)

        assert created_users[0].display_name == "loginuser"


class TestGitHubAuthorizeUrl:
    def test_authorize_url(self, github_provider, mock_settings):
        url = github_provider.get_authorize_url()
        assert "github.com/login/oauth/authorize" in url
        assert "client_id=gh-client-id" in url
        assert "redirect_uri=" in url
        assert "scope=user:email" in url
        assert "state=" not in url

    def test_authorize_url_with_state(self, github_provider, mock_settings):
        url = github_provider.get_authorize_url(state="xyz")
        assert "state=xyz" in url
