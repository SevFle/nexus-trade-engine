from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from engine.api.auth.base import AuthResult
from engine.api.auth.ldap import LDAPAuthProvider
from engine.db.models import User


@pytest.fixture
def provider():
    return LDAPAuthProvider()


class TestLDAPAuthProviderName:
    def test_name(self, provider):
        assert provider.name == "ldap"


class TestLDAPAuthenticate:
    async def test_missing_username_returns_error(self, provider, db_session):
        result = await provider.authenticate(password="pass", db=db_session)
        assert result.success is False
        assert "Username" in result.error

    async def test_missing_password_returns_error(self, provider, db_session):
        result = await provider.authenticate(username="user", db=db_session)
        assert result.success is False

    async def test_missing_db_returns_error(self, provider):
        result = await provider.authenticate(username="user", password="pass")
        assert result.success is False

    async def test_ldap_bind_failure_returns_error(self, provider, db_session):
        mock_ldap = MagicMock()
        mock_ldap.initialize.side_effect = Exception("connection refused")

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": MagicMock()}):
            result = await provider.authenticate(
                username="user", password="pass", db=db_session
            )
        assert result.success is False
        assert "Invalid credentials" in result.error

    async def test_successful_auth_creates_user(self, provider, db_session):
        mock_conn = MagicMock()
        mock_conn.search_s.return_value = [
            (
                "cn=user,ou=users,dc=example,dc=com",
                {
                    "uid": [b"testuser"],
                    "mail": [b"testuser@example.com"],
                    "cn": [b"Test User"],
                    "memberOf": [b"cn=users,ou=groups,dc=example,dc=com"],
                },
            )
        ]

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = mock_conn
        mock_ldap.SCOPE_SUBTREE = 2

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars.return_value = "testuser"

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="testuser", password="pass", db=db_session
            )

        assert result.success is True
        assert result.user_info.email == "testuser@example.com"
        assert result.user_info.provider == "ldap"
        assert result.user_info.external_id == "testuser"

    async def test_existing_user_authenticates(self, provider, db_session):
        user = User(
            email="testuser@example.com",
            hashed_password=None,
            display_name="Test User",
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )
        db_session.add(user)
        await db_session.flush()

        mock_conn = MagicMock()
        mock_conn.search_s.return_value = [
            (
                "cn=user,ou=users,dc=example,dc=com",
                {
                    "uid": [b"testuser"],
                    "mail": [b"testuser@example.com"],
                    "cn": [b"Test User"],
                    "memberOf": [],
                },
            )
        ]

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = mock_conn
        mock_ldap.SCOPE_SUBTREE = 2

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars.return_value = "testuser"

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="testuser", password="pass", db=db_session
            )

        assert result.success is True
        assert result.user_info.external_id == "testuser"

    async def test_disabled_user_returns_error(self, provider, db_session):
        user = User(
            email="disabled@example.com",
            hashed_password=None,
            display_name="Disabled",
            role="user",
            is_active=False,
            auth_provider="ldap",
            external_id="disabled_user",
        )
        db_session.add(user)
        await db_session.flush()

        mock_conn = MagicMock()
        mock_conn.search_s.return_value = [
            (
                "cn=user,ou=users,dc=example,dc=com",
                {
                    "uid": [b"disabled_user"],
                    "mail": [b"disabled@example.com"],
                    "cn": [b"Disabled"],
                    "memberOf": [],
                },
            )
        ]

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = mock_conn
        mock_ldap.SCOPE_SUBTREE = 2

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars.return_value = "disabled_user"

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="disabled_user", password="pass", db=db_session
            )

        assert result.success is False
        assert "disabled" in result.error.lower()

    async def test_empty_search_results_returns_error(self, provider, db_session):
        mock_conn = MagicMock()
        mock_conn.search_s.return_value = []

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = mock_conn
        mock_ldap.SCOPE_SUBTREE = 2

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars.return_value = "nouser"

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="nouser", password="pass", db=db_session
            )

        assert result.success is False
        assert "not found" in result.error.lower()

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

        mock_conn = MagicMock()
        mock_conn.search_s.return_value = [
            (
                "cn=user,ou=users,dc=example,dc=com",
                {
                    "uid": [b"newuser"],
                    "mail": [b"taken@example.com"],
                    "cn": [b"New User"],
                    "memberOf": [],
                },
            )
        ]

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = mock_conn
        mock_ldap.SCOPE_SUBTREE = 2

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars.return_value = "newuser"

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="newuser", password="pass", db=db_session
            )

        assert result.success is False
        assert "different provider" in result.error

    async def test_role_mapping_from_groups(self, provider, db_session):
        mock_conn = MagicMock()
        mock_conn.search_s.return_value = [
            (
                "cn=user,ou=users,dc=example,dc=com",
                {
                    "uid": [b"adminuser"],
                    "mail": [b"admin@example.com"],
                    "cn": [b"Admin User"],
                    "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
                },
            )
        ]

        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = mock_conn
        mock_ldap.SCOPE_SUBTREE = 2

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars.return_value = "adminuser"

        import json

        role_mapping = json.dumps({"admins": "admin"})

        with (
            patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}),
            patch("engine.api.auth.ldap.settings") as mock_settings,
        ):
            mock_settings.ldap_server_url = "ldap://localhost"
            mock_settings.ldap_bind_dn = "uid={{username}},ou=users,dc=example,dc=com"
            mock_settings.ldap_search_base = "ou=users,dc=example,dc=com"
            mock_settings.ldap_role_mapping = role_mapping

            result = await provider.authenticate(
                username="adminuser", password="pass", db=db_session
            )

        assert result.success is True
        assert "admin" in result.user_info.roles
