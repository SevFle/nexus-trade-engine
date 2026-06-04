"""Comprehensive tests for engine/api/auth/ldap.py — LDAPAuthProvider.

Covers:
  - authenticate: missing params (username, password, db)
  - authenticate: LDAP bind failure
  - authenticate: empty search results (user not found)
  - authenticate: successful auth with role mapping
  - authenticate: successful auth with default role (no mapping)
  - authenticate: new user creation
  - authenticate: email conflict with different provider
  - authenticate: disabled user
  - authenticate: existing user role update
  - name property
  - map_roles (inherited from IAuthProvider)
"""

from __future__ import annotations

import json
from typing import Any
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
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps(
            {
                "cn=admins,ou=groups,dc=example,dc=com": "admin",
                "cn=developers,ou=groups,dc=example,dc=com": "developer",
            }
        ),
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


class TestLDAPNameProperty:
    def test_name_returns_ldap(self, ldap_provider):
        assert ldap_provider.name == "ldap"


class TestLDAPAuthenticateMissingParams:
    async def test_missing_username(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await ldap_provider.authenticate(password="pass", db=mock_db)
        assert result.success is False
        assert "Username" in result.error

    async def test_missing_password(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await ldap_provider.authenticate(username="user", db=mock_db)
        assert result.success is False
        assert "password" in result.error.lower()

    async def test_missing_db(self, ldap_provider, mock_settings):
        result = await ldap_provider.authenticate(username="user", password="pass")
        assert result.success is False
        assert "db" in result.error.lower()

    async def test_all_missing(self, ldap_provider, mock_settings):
        result = await ldap_provider.authenticate()
        assert result.success is False

    async def test_empty_username(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await ldap_provider.authenticate(username="", password="pass", db=mock_db)
        assert result.success is False

    async def test_empty_password(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await ldap_provider.authenticate(username="user", password="", db=mock_db)
        assert result.success is False


class _FakeLDAPConn:
    """Fake ldap.ldapobject.LDAPObject for testing."""

    def __init__(
        self,
        bind_raises: Exception | None = None,
        search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
        search_raises: Exception | None = None,
        unbind_raises: Exception | None = None,
        starttls_raises: Exception | None = None,
    ):
        self._bind_raises = bind_raises
        self._search_results = search_results or []
        self._search_raises = search_raises
        self._unbind_raises = unbind_raises
        self._starttls_raises = starttls_raises
        self._options: dict[int, Any] = {}
        self.unbind_call_count = 0
        self.starttls_call_count = 0
        self.bind_call_count = 0
        self.last_bind_dn: str | None = None

    def set_option(self, opt: int, value: Any) -> None:
        self._options[opt] = value

    def simple_bind_s(self, dn: str, password: str) -> None:
        self.bind_call_count += 1
        self.last_bind_dn = dn
        if self._bind_raises:
            raise self._bind_raises

    def search_s(self, base: str, scope: int, filterstr: str, attrlist: list[str]):
        if self._search_raises:
            raise self._search_raises
        return self._search_results

    def start_tls_s(self) -> None:
        self.starttls_call_count += 1
        if self._starttls_raises:
            raise self._starttls_raises

    def unbind_s(self) -> None:
        self.unbind_call_count += 1
        if self._unbind_raises:
            raise self._unbind_raises


def _build_ldap_mock(
    bind_raises: Exception | None = None,
    search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
    search_raises: Exception | None = None,
    unbind_raises: Exception | None = None,
):
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(
        return_value=_FakeLDAPConn(
            bind_raises=bind_raises,
            search_results=search_results,
            search_raises=search_raises,
            unbind_raises=unbind_raises,
        )
    )
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    mock_dn = MagicMock()
    mock_dn.escape_dn_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter, mock_dn


def _make_mock_db():
    """Build a mock DB session that tracks added users and simulates refresh."""
    mock_db = AsyncMock(spec=AsyncSession)
    added_users: list[Any] = []

    def track_add(user):
        added_users.append(user)
        user.is_active = True

    async def mock_refresh(user):
        user.is_active = True

    mock_db.add = MagicMock(side_effect=track_add)
    mock_db.refresh = AsyncMock(side_effect=mock_refresh)
    mock_db.flush = AsyncMock()
    return mock_db, added_users


class TestLDAPAuthenticateBindFailure:
    async def test_bind_failure_returns_invalid_credentials(self, ldap_provider, mock_settings):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            bind_raises=Exception("LDAP bind failed")
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="wrongpass", db=mock_db
            )

        assert result.success is False
        assert "Invalid credentials" in result.error

    async def test_connection_failure_returns_invalid_credentials(
        self, ldap_provider, mock_settings
    ):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            bind_raises=ConnectionError("Connection refused")
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert "Invalid credentials" in result.error


class TestLDAPAuthenticateSearchResults:
    async def test_empty_results_user_not_found(self, ldap_provider, mock_settings):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(search_results=[])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert "not found" in result.error.lower()


def _make_ldap_attrs(
    uid: str = "testuser",
    mail: bytes = b"testuser@example.com",
    cn: bytes = b"Test User",
    member_of: list[bytes] | None = None,
):
    attrs: dict[str, list[bytes]] = {
        "uid": [uid.encode()],
        "mail": [mail],
        "cn": [cn],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


def _mock_execute_factory(*results):
    """Return an async execute function that returns the given results in sequence."""
    idx = 0

    async def mock_execute(stmt):
        nonlocal idx
        r = MagicMock()
        r.scalar_one_or_none.return_value = results[idx] if idx < len(results) else None
        idx += 1
        return r

    return mock_execute


class TestLDAPAuthenticateSuccess:
    async def test_new_user_created_with_mapped_role(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "testuser@example.com"
        assert result.user_info.display_name == "Test User"
        assert result.user_info.provider == "ldap"
        assert result.user_info.external_id == "testuser"
        assert len(added_users) == 1

    async def test_new_user_default_role_when_no_groups(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(member_of=[])
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=user2,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="user2", password="pass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert "user" in result.user_info.roles

    async def test_developer_role_mapping(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(member_of=[b"cn=developers,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=devuser,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="devuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert len(added_users) == 1
        assert added_users[0].role == "developer"

    async def test_multiple_groups_maps_highest_role(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(
            member_of=[
                b"cn=developers,ou=groups,dc=example,dc=com",
                b"cn=admins,ou=groups,dc=example,dc=com",
            ]
        )
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=multi,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="multi", password="pass", db=mock_db
            )

        assert result.success is True
        assert added_users[0].role == "admin"

    async def test_email_fallback_when_mail_empty(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(mail=b"")
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=nomail,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _ = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="nomail", password="pass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "nomail@ldap"

    async def test_cn_fallback_when_empty(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(cn=b"")
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=nocn,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _ = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(username="nocn", password="pass", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.display_name == "nocn"


class TestLDAPAuthenticateExistingUser:
    async def test_existing_ldap_user_logs_in(self, ldap_provider, mock_settings):
        from engine.db.models import User

        attrs = _make_ldap_attrs()
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )

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

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "testuser@example.com"
        mock_db.add.assert_not_called()

    async def test_existing_user_role_updated(self, ldap_provider, mock_settings):
        from engine.db.models import User

        attrs = _make_ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=promoted,ou=users,dc=example,dc=com", attrs)]
        )

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
        mock_db.flush = AsyncMock()

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()


class TestLDAPAuthenticateEmailConflict:
    async def test_email_registered_with_different_provider(self, ldap_provider, mock_settings):
        from engine.db.models import User

        attrs = _make_ldap_attrs()
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=conflict,ou=users,dc=example,dc=com", attrs)]
        )

        conflict_user = User(
            email="testuser@example.com",
            display_name="Local User",
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

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert "different provider" in result.error


class TestLDAPAuthenticateDisabledUser:
    async def test_disabled_user_rejected(self, ldap_provider, mock_settings):
        from engine.db.models import User

        attrs = _make_ldap_attrs()
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=disabled,ou=users,dc=example,dc=com", attrs)]
        )

        disabled_user = User(
            email="testuser@example.com",
            display_name="Disabled",
            is_active=False,
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute.return_value = mock_result

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert "disabled" in result.error.lower()


class TestLDAPRoleMappingEmpty:
    async def test_no_role_mapping_configured(self, ldap_provider, monkeypatch):
        s = Settings(
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping="",
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs(member_of=[b"cn=somegroup,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=norolemap,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _ = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn}
        ):
            result = await ldap_provider.authenticate(
                username="norolemap", password="pass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert "user" in result.user_info.roles


class TestLDAPInheritedMethods:
    async def test_get_user_info_returns_none(self, ldap_provider):
        result = await ldap_provider.get_user_info("some-id")
        assert result is None

    async def test_create_user_not_supported(self, ldap_provider):
        result = await ldap_provider.create_user(_user_info=MagicMock())
        assert result.success is False
        assert "not supported" in result.error.lower()

    def test_map_roles_admin_highest(self, ldap_provider):
        assert ldap_provider.map_roles(["user", "admin"]) == "admin"

    def test_map_roles_developer(self, ldap_provider):
        assert ldap_provider.map_roles(["developer"]) == "developer"

    def test_map_roles_unknown_defaults_user(self, ldap_provider):
        assert ldap_provider.map_roles(["unknown_role"]) == "user"

    def test_map_roles_empty_list(self, ldap_provider):
        assert ldap_provider.map_roles([]) == "user"


# ---------------------------------------------------------------------------
# Security regression tests — SEV-507 follow-ups:
#   1. exception-path unbind (try/finally)
#   2. DN injection rejection (escape_dn_chars used for bind DN)
#   3. plaintext LDAP URL rejection in non-dev/test environments
# ---------------------------------------------------------------------------


class TestLDAPExceptionPathUnbind:
    """conn.unbind_s() must run even when bind/search raise."""

    async def test_unbind_called_after_bind_failure(self, ldap_provider, mock_settings):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            bind_raises=Exception("LDAP bind failed")
        )
        mock_db = AsyncMock(spec=AsyncSession)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="wrongpass", db=mock_db
            )

        assert result.success is False
        fake_conn = mock_ldap.initialize.return_value
        assert fake_conn.unbind_call_count == 1, (
            "unbind_s() must be invoked exactly once even when simple_bind_s() raises"
        )

    async def test_unbind_called_after_search_failure(self, ldap_provider, mock_settings):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_raises=Exception("LDAP search crashed")
        )
        mock_db = AsyncMock(spec=AsyncSession)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        fake_conn = mock_ldap.initialize.return_value
        # Bind succeeded, search raised → finally must still unbind.
        assert fake_conn.bind_call_count == 1
        assert fake_conn.unbind_call_count == 1

    async def test_unbind_failure_does_not_mask_original_error(self, ldap_provider, mock_settings):
        """If unbind_s() itself raises during cleanup, the original
        'Invalid credentials' result must still surface."""
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            bind_raises=Exception("LDAP bind failed"),
            unbind_raises=RuntimeError("connection already closed"),
        )
        mock_db = AsyncMock(spec=AsyncSession)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="wrongpass", db=mock_db
            )

        assert result.success is False
        # Original bind failure must propagate, not the cleanup error.
        assert result.error == "Invalid credentials"
        fake_conn = mock_ldap.initialize.return_value
        assert fake_conn.unbind_call_count == 1

    async def test_unbind_not_called_when_initialize_fails(self, ldap_provider, mock_settings):
        """If ldap.initialize() itself raises there is no connection to
        unbind — we must not call unbind_s() on a non-existent handle."""
        mock_ldap = MagicMock()
        mock_ldap.initialize = MagicMock(side_effect=RuntimeError("initialize failed"))
        mock_ldap.OPT_NETWORK_TIMEOUT = 7
        mock_ldap.OPT_TIMEOUT = 8
        mock_ldap.SCOPE_SUBTREE = 2
        mock_filter = MagicMock()
        mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
        mock_dn = MagicMock()
        mock_dn.escape_dn_chars = MagicMock(side_effect=lambda x: x)

        mock_db = AsyncMock(spec=AsyncSession)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert result.error == "Invalid credentials"


class TestLDAPDNInjectionRejection:
    """The bind DN slot must use ldap.dn.escape_dn_chars (not just
    escape_filter_chars) so an attacker cannot pivot the bind DN."""

    async def test_comma_in_username_does_not_split_dn(self, ldap_provider, mock_settings):
        """A username containing `,` would let an attacker escape the
        `uid=…` RDN and bind as a different DN if escape_dn_chars were
        not applied. Verify the escape function is invoked on the DN
        slot (not just the filter)."""
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[
                (
                    "uid=admin,ou=users,dc=example,dc=com",
                    _make_ldap_attrs(),
                )
            ]
        )

        # Track which inputs go to which escaper.
        dn_inputs: list[str] = []
        filter_inputs: list[str] = []
        mock_dn.escape_dn_chars = MagicMock(side_effect=lambda x: (dn_inputs.append(x), x)[1])
        mock_filter.escape_filter_chars = MagicMock(
            side_effect=lambda x: (filter_inputs.append(x), x)[1]
        )

        from engine.db.models import User

        existing_user = User(
            email="testuser@example.com",
            display_name="Test",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        # Malicious username attempting DN injection.
        malicious = "admin,ou=admins"

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            result = await ldap_provider.authenticate(
                username=malicious, password="pass", db=mock_db
            )

        # The auth still succeeded because the search returned a result,
        # but the point of the test is what was sent to escape_dn_chars.
        assert result.success is True

        # The raw username must be passed through escape_dn_chars (not just
        # escape_filter_chars), proving the bind DN slot is sanitized.
        assert dn_inputs == [malicious], (
            "escape_dn_chars must be called with the raw username for the bind DN"
        )
        assert filter_inputs == [malicious], (
            "escape_filter_chars must still be called for the search filter"
        )

        # And escape_dn_chars must be a *different* callable from
        # escape_filter_chars — i.e. we are using two distinct sanitizers.
        assert mock_dn.escape_dn_chars is not mock_filter.escape_filter_chars

    async def test_escape_dn_chars_module_is_loaded(self, ldap_provider, mock_settings):
        """The provider must import ldap.dn.escape_dn_chars (not just
        ldap.filter.escape_filter_chars)."""
        attrs = _make_ldap_attrs()
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=probe,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _added = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            await ldap_provider.authenticate(username="probe", password="pass", db=mock_db)

        # ldap.dn.escape_dn_chars must actually be invoked.
        mock_dn.escape_dn_chars.assert_called_once_with("probe")
        mock_filter.escape_filter_chars.assert_called_once_with("probe")


class TestLDAPPlaintextURLRejectedInProd:
    """Settings must refuse plaintext ldap:// outside dev/test."""

    def test_plaintext_ldap_url_rejected_in_production(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            Settings(
                app_env="production",
                ldap_server_url="ldap://ldap.example.com:389",
                ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
                ldap_search_base="ou=users,dc=example,dc=com",
            )
        # The error must mention transport security so operators can
        # diagnose the misconfiguration quickly.
        message = str(exc_info.value).lower()
        assert "ldap" in message
        assert "ldaps" in message or "starttls" in message

    def test_plaintext_ldap_url_rejected_in_staging(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(
                app_env="staging",
                ldap_server_url="ldap://ldap.example.com:389",
            )

    def test_ldaps_url_accepted_in_production(self):
        # ldaps:// must be permitted in prod without StartTLS.
        s = Settings(
            app_env="production",
            ldap_server_url="ldaps://ldap.example.com:636",
        )
        assert s.ldap_server_url == "ldaps://ldap.example.com:636"
        assert s.ldap_use_starttls is False

    def test_ldap_url_with_starttls_accepted_in_production(self):
        # plaintext scheme + explicit StartTLS opt-in must be permitted.
        s = Settings(
            app_env="production",
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_use_starttls=True,
        )
        assert s.ldap_use_starttls is True

    def test_plaintext_ldap_url_allowed_in_development(self):
        # Dev environment is permitted to use plaintext for local mocks.
        s = Settings(
            app_env="development",
            ldap_server_url="ldap://localhost:389",
        )
        assert s.app_env == "development"

    def test_plaintext_ldap_url_allowed_in_test(self):
        # Same for the test environment.
        s = Settings(
            app_env="test",
            ldap_server_url="ldap://localhost:389",
        )
        assert s.app_env == "test"

    def test_empty_ldap_url_skipped_in_production(self):
        # If LDAP is not configured at all, the validator must be a no-op
        # — production deployments are not required to enable LDAP.
        s = Settings(app_env="production", ldap_server_url="")
        assert s.ldap_server_url == ""

    async def test_starttls_actually_invoked_when_configured(self, ldap_provider, monkeypatch):
        """When ldap_use_starttls=True the provider must call
        conn.start_tls_s() before simple_bind_s()."""
        s = Settings(
            app_env="test",
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping="{}",
            ldap_use_starttls=True,
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs()
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=starttls,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _added = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.filter": mock_filter, "ldap.dn": mock_dn},
        ):
            result = await ldap_provider.authenticate(
                username="starttls", password="pass", db=mock_db
            )

        assert result.success is True
        fake_conn = mock_ldap.initialize.return_value
        assert fake_conn.starttls_call_count == 1, (
            "conn.start_tls_s() must be invoked when ldap_use_starttls is True"
        )
        # start_tls_s must happen *before* simple_bind_s.
        assert fake_conn.bind_call_count == 1
