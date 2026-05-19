"""Comprehensive tests for engine/api/auth/ldap.py.

Covers:
  - name property
  - authenticate (missing params, bind failure, user not found,
    happy path new user, existing user with role sync, email conflict,
    disabled user, group-to-role mapping, default role fallback,
    role sync on subsequent login)
  - get_authorize_url returns empty (LDAP is not OAuth)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.ldap import LDAPAuthProvider
from engine.config import Settings


@pytest.fixture
def ldap_provider():
    return LDAPAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_bind_password="",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping='{"cn=admins,ou=groups,dc=example,dc=com": "admin", '
        '"cn=developers,ou=groups,dc=example,dc=com": "developer"}',
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


LDAP_USER_ATTRS = {
    "uid": [b"testuser"],
    "mail": [b"testuser@example.com"],
    "cn": [b"Test User"],
    "memberOf": [
        b"cn=admins,ou=groups,dc=example,dc=com",
        b"cn=developers,ou=groups,dc=example,dc=com",
    ],
}


def _make_mock_ldap(attrs=None, search_results=None, bind_error=None):
    mock_conn = MagicMock()
    if bind_error:
        mock_conn.simple_bind_s.side_effect = bind_error
    else:
        mock_conn.simple_bind_s.return_value = (97, [], 2, [])

    results = search_results if search_results is not None else [("dn", attrs or LDAP_USER_ATTRS)]
    mock_conn.search_s.return_value = results
    mock_conn.unbind_s.return_value = None
    return mock_conn


class TestLDAPNameProperty:
    def test_name_returns_ldap(self, ldap_provider):
        assert ldap_provider.name == "ldap"


class TestLDAPAuthenticate:
    async def test_authenticate_missing_username(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await ldap_provider.authenticate(password="pass", db=mock_db)
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_missing_password(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await ldap_provider.authenticate(username="user", db=mock_db)
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_missing_db(self, ldap_provider, mock_settings):
        result = await ldap_provider.authenticate(username="user", password="pass")
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_missing_all(self, ldap_provider, mock_settings):
        result = await ldap_provider.authenticate()
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_bind_failure(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(
            bind_error=Exception("Invalid credentials"),
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="wrongpass", db=mock_db,
            )

        assert result.success is False
        assert "Invalid credentials" in result.error

    async def test_authenticate_user_not_found_in_ldap(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(search_results=[])
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db,
            )

        assert result.success is False
        assert "not found" in result.error

    async def test_authenticate_happy_path_new_user_admin_role(
        self, ldap_provider, mock_settings
    ):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "testuser@example.com"
        assert result.user_info.provider == "ldap"
        assert result.user_info.external_id == "testuser"
        assert result.user_info.display_name == "Test User"
        assert result.user_info.roles == ["admin"]
        mock_db.add.assert_called_once()

    async def test_authenticate_existing_user_role_sync(
        self, ldap_provider, mock_settings
    ):
        from engine.db.models import User

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        existing_user = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()
        mock_db.add.assert_not_called()

    async def test_authenticate_existing_user_no_role_change(
        self, ldap_provider, mock_settings
    ):
        from engine.db.models import User

        attrs = {
            "uid": [b"testuser"],
            "mail": [b"testuser@example.com"],
            "cn": [b"Test User"],
            "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        existing_user = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )

        assert result.success is True
        assert existing_user.role == "admin"

    async def test_authenticate_email_conflict_different_provider(
        self, ldap_provider, mock_settings
    ):
        from engine.db.models import User

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

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

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )

        assert result.success is False
        assert "different provider" in result.error

    async def test_authenticate_disabled_user(self, ldap_provider, mock_settings):
        from engine.db.models import User

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        disabled_user = User(
            email="testuser@example.com",
            display_name="Disabled User",
            is_active=False,
            role="admin",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute.return_value = mock_result

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )

        assert result.success is False
        assert "disabled" in result.error

    async def test_authenticate_default_role_when_no_groups_match(
        self, ldap_provider, mock_settings
    ):
        attrs = {
            "uid": [b"newuser"],
            "mail": [b"newuser@example.com"],
            "cn": [b"New User"],
            "memberOf": [b"cn=some-group,ou=groups,dc=example,dc=com"],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="newuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.roles == ["user"]

    async def test_authenticate_developer_role_mapping(self, ldap_provider, mock_settings):
        attrs = {
            "uid": [b"devuser"],
            "mail": [b"dev@example.com"],
            "cn": [b"Dev User"],
            "memberOf": [b"cn=developers,ou=groups,dc=example,dc=com"],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="devuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.roles == ["developer"]

    async def test_authenticate_no_member_of_attribute(self, ldap_provider, mock_settings):
        attrs = {
            "uid": [b"plainuser"],
            "mail": [b"plain@example.com"],
            "cn": [b"Plain User"],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="plainuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.roles == ["user"]

    async def test_authenticate_mail_fallback_when_empty(
        self, ldap_provider, mock_settings
    ):
        attrs = {
            "uid": [b"nomailuser"],
            "mail": [b""],
            "cn": [b"No Mail User"],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="nomailuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.email == "nomailuser@ldap"

    async def test_authenticate_cn_fallback_when_empty(
        self, ldap_provider, mock_settings
    ):
        attrs = {
            "uid": [b"nocnuser"],
            "mail": [b"nocn@example.com"],
            "cn": [b""],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="nocnuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.display_name == "nocnuser"

    async def test_authenticate_empty_role_mapping_config(
        self, ldap_provider, mock_settings
    ):
        mock_settings.ldap_role_mapping = "{}"

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.roles == ["user"]

    async def test_authenticate_escapes_username(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars") as mock_escape:
            mock_escape.side_effect = lambda x: x.replace("*", "\\2a")
            await ldap_provider.authenticate(
                username="test*user", password="pass", db=mock_db,
            )

        mock_escape.assert_called_once_with("test*user")


class TestLDAPAuthorizeUrl:
    async def test_get_authorize_url_returns_empty(self, ldap_provider):
        url = await ldap_provider.get_authorize_url()
        assert url == ""

    async def test_get_authorize_url_with_state_returns_empty(self, ldap_provider):
        url = await ldap_provider.get_authorize_url(state="some-state")
        assert url == ""
