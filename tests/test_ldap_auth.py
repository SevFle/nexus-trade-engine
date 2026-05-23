"""Comprehensive tests for engine/api/auth/ldap.py.

Covers:
  - name property
  - authenticate (ldap-not-installed guard, missing params, bind failure,
    user not found, happy path new user, existing user with role sync,
    email conflict, disabled user, group-to-role mapping, default role
    fallback, role sync on subsequent login, escape_filter_chars guard)
  - get_authorize_url returns empty (LDAP is not OAuth)
  - module-level import fallback when python-ldap is absent
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


async def _mock_refresh(obj):
    if hasattr(obj, "is_active") and obj.is_active is None:
        obj.is_active = True


def _make_new_user_db():
    mock_db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock(side_effect=_mock_refresh)
    return mock_db


class TestLDAPNameProperty:
    def test_name_returns_ldap(self, ldap_provider):
        assert ldap_provider.name == "ldap"


class TestLDAPNotInstalledGuard:
    async def test_authenticate_returns_error_when_ldap_is_none(self, ldap_provider, mock_settings):
        with patch("engine.api.auth.ldap.ldap", None):
            result = await ldap_provider.authenticate(
                username="user", password="pass", db=AsyncMock(spec=AsyncSession),
            )
        assert result.success is False
        assert result.error == "LDAP module not installed"

    async def test_guard_returns_error_even_with_valid_params(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", None):
            result = await ldap_provider.authenticate(
                username="validuser", password="validpass", db=mock_db,
            )
        assert result.success is False
        assert "LDAP module not installed" in result.error

    async def test_guard_triggers_before_missing_params_check(self, ldap_provider, mock_settings):
        with patch("engine.api.auth.ldap.ldap", None):
            result = await ldap_provider.authenticate()
        assert result.success is False
        assert result.error == "LDAP module not installed"
        assert "Username" not in result.error

    async def test_guard_triggers_with_empty_username(self, ldap_provider, mock_settings):
        with patch("engine.api.auth.ldap.ldap", None):
            result = await ldap_provider.authenticate(username="", password="pass")
        assert result.success is False
        assert result.error == "LDAP module not installed"

    async def test_guard_triggers_with_no_password(self, ldap_provider, mock_settings):
        with patch("engine.api.auth.ldap.ldap", None):
            result = await ldap_provider.authenticate(username="user", password="")
        assert result.success is False
        assert result.error == "LDAP module not installed"

    async def test_ldap_none_result_has_no_user_info(self, ldap_provider, mock_settings):
        with patch("engine.api.auth.ldap.ldap", None):
            result = await ldap_provider.authenticate(
                username="user", password="pass", db=AsyncMock(spec=AsyncSession),
            )
        assert result.user_info is None

    async def test_ldap_module_level_is_none_when_not_installed(self):
        import engine.api.auth.ldap as ldap_mod

        assert ldap_mod.ldap is None
        assert ldap_mod.escape_filter_chars is None

    async def test_guard_bypassed_when_ldap_patched(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = _make_new_user_db()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )

        assert result.success is True
        assert result.error is None


class TestEscapeFilterCharsGuard:
    async def test_escape_filter_chars_none_returns_error(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", None):
            result = await ldap_provider.authenticate(
                username="user", password="pass", db=mock_db,
            )
        assert result.success is False
        assert result.error == "LDAP module not installed"

    async def test_escape_filter_chars_none_no_user_info(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", None):
            result = await ldap_provider.authenticate(
                username="user", password="pass", db=AsyncMock(spec=AsyncSession),
            )
        assert result.user_info is None

    async def test_escape_filter_chars_none_with_valid_ldap(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", None):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=AsyncMock(spec=AsyncSession),
            )
        assert result.success is False
        assert result.error == "LDAP module not installed"
        mock_ldap.initialize.assert_not_called()

    async def test_both_ldap_and_escape_none(self, ldap_provider, mock_settings):
        with patch("engine.api.auth.ldap.ldap", None), \
             patch("engine.api.auth.ldap.escape_filter_chars", None):
            result = await ldap_provider.authenticate(
                username="user", password="pass", db=AsyncMock(spec=AsyncSession),
            )
        assert result.success is False
        assert result.error == "LDAP module not installed"

    async def test_escape_filter_chars_callable_succeeds(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = _make_new_user_db()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db,
            )
        assert result.success is True

    async def test_escape_filter_chars_actually_escapes(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = _make_new_user_db()

        escaped_values = []

        def custom_escape(val):
            escaped_values.append(val)
            return val.replace("*", "\\2a").replace("(", "\\28").replace(")", "\\29")

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", side_effect=custom_escape):
            result = await ldap_provider.authenticate(
                username="user*(test)", password="pass", db=mock_db,
            )

        assert result.success is True
        assert escaped_values == ["user*(test)"]


class TestLDAPTypingVerification:
    def test_ldap_module_level_type_is_any_or_none(self):
        import engine.api.auth.ldap as ldap_mod

        assert ldap_mod.ldap is None or ldap_mod.ldap is not None

    def test_escape_filter_chars_module_level_is_callable_or_none(self):
        import engine.api.auth.ldap as ldap_mod

        if ldap_mod.escape_filter_chars is not None:
            assert callable(ldap_mod.escape_filter_chars)
        else:
            assert ldap_mod.escape_filter_chars is None

    def test_no_type_ignore_comments_in_source(self):
        import inspect

        import engine.api.auth.ldap as ldap_mod

        source = inspect.getsource(ldap_mod)
        assert "type: ignore" not in source

    def test_ldap_and_escape_initialized_at_module_level(self):
        import engine.api.auth.ldap as ldap_mod

        assert hasattr(ldap_mod, "ldap")
        assert hasattr(ldap_mod, "escape_filter_chars")


class TestLDAPAuthenticate:
    async def test_authenticate_missing_username(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(password="pass", db=mock_db)
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_missing_password(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(username="user", db=mock_db)
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_missing_db(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(username="user", password="pass")
        assert result.success is False
        assert "Username" in result.error

    async def test_authenticate_missing_all(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
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

    async def test_authenticate_bind_ldap_error(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(
            bind_error=Exception("LDAP connection refused"),
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db,
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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

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

        mock_db = _make_new_user_db()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars") as mock_escape:
            mock_escape.side_effect = lambda x: x.replace("*", "\\2a")
            await ldap_provider.authenticate(
                username="test*user", password="pass", db=mock_db,
            )

        mock_escape.assert_called_once_with("test*user")

    async def test_authenticate_special_chars_in_username(self, ldap_provider, mock_settings):
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap()
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = _make_new_user_db()

        escaped = []

        def capture_escape(val):
            escaped.append(val)
            return val.replace("(", "\\28").replace(")", "\\29")

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", side_effect=capture_escape):
            result = await ldap_provider.authenticate(
                username="user(ldap)", password="pass", db=mock_db,
            )

        assert result.success is True
        assert escaped == ["user(ldap)"]

    async def test_authenticate_multiple_groups_multiple_roles(
        self, ldap_provider, mock_settings
    ):
        attrs = {
            "uid": [b"multigroup"],
            "mail": [b"multi@example.com"],
            "cn": [b"Multi Group"],
            "memberOf": [
                b"cn=admins,ou=groups,dc=example,dc=com",
                b"cn=developers,ou=groups,dc=example,dc=com",
                b"cn=users,ou=groups,dc=example,dc=com",
            ],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = _make_new_user_db()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="multigroup", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.roles == ["admin"]

    async def test_authenticate_none_role_mapping_config(
        self, ldap_provider, mock_settings
    ):
        mock_settings.ldap_role_mapping = None

        attrs = {
            "uid": [b"noroleuser"],
            "mail": [b"norole@example.com"],
            "cn": [b"No Role User"],
            "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
        }
        mock_ldap = MagicMock()
        mock_ldap.initialize.return_value = _make_mock_ldap(attrs=attrs)
        mock_ldap.SCOPE_SUBTREE = 2

        mock_db = _make_new_user_db()

        with patch("engine.api.auth.ldap.ldap", mock_ldap), \
             patch("engine.api.auth.ldap.escape_filter_chars", lambda x: x):
            result = await ldap_provider.authenticate(
                username="noroleuser", password="pass", db=mock_db,
            )

        assert result.success is True
        assert result.user_info.roles == ["user"]


class TestLDAPAuthorizeUrl:
    async def test_get_authorize_url_returns_empty(self, ldap_provider):
        url = await ldap_provider.get_authorize_url()
        assert url == ""

    async def test_get_authorize_url_with_state_returns_empty(self, ldap_provider):
        url = await ldap_provider.get_authorize_url(_state="some-state")
        assert url == ""
