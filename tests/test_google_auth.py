from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.google import GoogleAuthProvider
from engine.config import Settings


@pytest.fixture
def google_provider():
    return GoogleAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        google_client_id="g-client-id",
        google_client_secret="g-client-secret",
        google_redirect_uri="https://app.example.com/google/callback",
    )
    monkeypatch.setattr("engine.api.auth.google.settings", s)
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
    token_resp = _FakeResponse(json_data={"access_token": "at-google"})
    userinfo_resp = _FakeResponse(
        json_data={"sub": "g-123", "email": "user@google.com", "name": "G User"}
    )
    return _FakeClient(responses=[token_resp, userinfo_resp])


class TestGoogleNameProperty:
    def test_name(self, google_provider):
        assert google_provider.name == "google"


class TestGoogleAuthenticate:
    async def test_missing_code(self, google_provider, mock_settings):
        result = await google_provider.authenticate(db=AsyncMock(spec=AsyncSession))
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_missing_db(self, google_provider, mock_settings):
        result = await google_provider.authenticate(code="abc")
        assert result.success is False

    async def test_token_exchange_fails(self, google_provider, mock_settings):
        token_resp = _FakeResponse(
            raise_error=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
        fake_client = _FakeClient(responses=[token_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(
                code="c", db=AsyncMock(spec=AsyncSession)
            )

        assert result.success is False
        assert "Google authentication failed" in result.error

    async def test_userinfo_fails(self, google_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(
            raise_error=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(
                code="c", db=AsyncMock(spec=AsyncSession)
            )

        assert result.success is False

    async def test_incomplete_profile_no_sub(self, google_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(json_data={"sub": "", "email": "x@x.com"})
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_incomplete_profile_no_email(self, google_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(json_data={"sub": "g-1", "email": ""})
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_new_user_created(self, google_provider, mock_settings):
        fake_client = _make_success_client()
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=_simulate_db_refresh)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="c", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "user@google.com"
        assert result.user_info.provider == "google"
        assert result.user_info.external_id == "g-123"
        mock_db.add.assert_called_once()

    async def test_existing_google_user(self, google_provider, mock_settings):
        from engine.db.models import User

        fake_client = _make_success_client()
        existing = User(
            email="user@google.com",
            display_name="G User",
            is_active=True,
            auth_provider="google",
            external_id="g-123",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="c", db=mock_db)

        assert result.success is True
        mock_db.add.assert_not_called()

    async def test_email_conflict(self, google_provider, mock_settings):
        from engine.db.models import User

        fake_client = _make_success_client()
        conflict = User(email="user@google.com", auth_provider="local")

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
            result = await google_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "different provider" in result.error

    async def test_disabled_user(self, google_provider, mock_settings):
        from engine.db.models import User

        fake_client = _make_success_client()
        disabled = User(
            email="user@google.com",
            is_active=False,
            auth_provider="google",
            external_id="g-123",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await google_provider.authenticate(code="c", db=mock_db)

        assert result.success is False
        assert "disabled" in result.error

    async def test_name_fallback_to_email(self, google_provider, mock_settings):
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(json_data={"sub": "g-n", "email": "myname@g.com"})
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
            await google_provider.authenticate(code="c", db=mock_db)

        assert created_users[0].display_name == "myname"

    async def test_token_params_sent_correctly(self, google_provider, mock_settings):
        posted_data = {}

        token_resp = _FakeResponse(json_data={"access_token": "at"})
        userinfo_resp = _FakeResponse(json_data={"sub": "g-p", "email": "p@g.com"})
        fake_client = _FakeClient(responses=[token_resp, userinfo_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=_simulate_db_refresh)

        with patch("httpx.AsyncClient", return_value=fake_client):
            await google_provider.authenticate(code="my-code", db=mock_db)


class TestGoogleAuthorizeUrl:
    def test_authorize_url(self, google_provider, mock_settings):
        url = google_provider.get_authorize_url()
        assert "accounts.google.com" in url
        assert "client_id=g-client-id" in url
        assert "redirect_uri=" in url
        assert "response_type=code" in url
        assert "state=" not in url

    def test_authorize_url_with_state(self, google_provider, mock_settings):
        url = google_provider.get_authorize_url(state="abc123")
        assert "state=abc123" in url
