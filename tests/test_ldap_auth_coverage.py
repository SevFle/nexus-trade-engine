"""Extra behavioural/regression tests for the recently refactored
``engine/api/auth/ldap.py``.

The companion ``tests/test_ldap_auth.py`` already reaches 100% *line*
coverage, but several behaviours introduced by the recent refactor are only
exercised indirectly — and the refactor's centrepiece (the new
``LDAPError`` base class) is asserted only against ``Exception``, never
against ``LDAPError`` itself. This file fills those gaps with targeted unit
tests for:

* The ``LDAPError`` hierarchy — both subclasses MUST inherit from
  ``LDAPError`` so callers can ``except LDAPError`` to catch every LDAP
  failure (the broad-catch contract promised in the module docstrings).
* ``_decode_first`` — the new safe-indexing helper, exercised directly.
* ``LDAPAuthProvider._map_ldap_groups_to_role`` — the extracted role-mapping
  method, exercised directly with no LDAP connection.
* ``LDAPAuthProvider._resolve_user`` — the extracted user-resolution method,
  including the subtle "role unchanged -> no flush" branch.
* LDAP-injection prevention — ``escape_filter_chars`` MUST be invoked with the
  raw username and the escaped value MUST flow into both the bind DN and the
  search filter (security-critical).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.ldap import (
    LDAPAuthProvider,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServiceUnavailableError,
    _decode_first,
)
from engine.config import Settings


# ---------------------------------------------------------------------------
# Shared fixtures (kept local so this file is self-contained).
# ---------------------------------------------------------------------------
@pytest.fixture
def ldap_provider() -> LDAPAuthProvider:
    return LDAPAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch) -> Settings:
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


class _FakeLDAPError(Exception):
    pass


class _FakeInvalidCredentialsError(_FakeLDAPError):
    pass


class _FakeLDAPConn:
    def __init__(self) -> None:
        self.bound_with: tuple[str, str] | None = None
        self.search_args: tuple | None = None

    def set_option(self, opt: int, value: Any) -> None:
        pass

    def simple_bind_s(self, dn: str, password: str) -> None:
        self.bound_with = (dn, password)

    def search_s(self, base: str, scope: int, filterstr: str, attrlist: list[str]):
        self.search_args = (base, scope, filterstr, attrlist)
        return [("uid=testuser,ou=users,dc=example,dc=com", {"uid": [b"testuser"]})]

    def unbind_s(self) -> None:
        pass


def _recording_ldap_mock(conn: _FakeLDAPConn):
    """A mock ldap module that exposes real exception types and records the
    bind/search arguments so tests can assert on them."""
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=conn)
    mock_ldap.LDAPError = _FakeLDAPError
    mock_ldap.INVALID_CREDENTIALS = _FakeInvalidCredentialsError
    mock_ldap.SERVER_DOWN = _FakeLDAPError
    mock_ldap.TIMEOUT = _FakeLDAPError
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    # Record what was passed to escape_filter_chars so we can assert the raw
    # username flows through it.
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


# ===========================================================================
# 1. LDAPError hierarchy — the refactor's centrepiece.
#
# The module docstrings promise that callers can catch ``LDAPError`` broadly
# or each subclass specifically. The existing suite only checks
# ``issubclass(..., Exception)``; this asserts the real contract.
# ===========================================================================
class TestLDAPErrorHierarchy:
    def test_invalid_credentials_inherits_from_ldap_error(self):
        assert issubclass(LDAPInvalidCredentialsError, LDAPError)

    def test_service_unavailable_inherits_from_ldap_error(self):
        assert issubclass(LDAPServiceUnavailableError, LDAPError)

    def test_ldap_error_itself_is_an_exception(self):
        assert issubclass(LDAPError, Exception)

    def test_catching_base_ldap_error_catches_credential_error(self):
        """A caller writing ``except LDAPError`` must catch a credential
        failure — this is the broad-catch contract."""
        with pytest.raises(LDAPError):
            raise LDAPInvalidCredentialsError("nope")

    def test_catching_base_ldap_error_catches_service_error(self):
        with pytest.raises(LDAPError):
            raise LDAPServiceUnavailableError("down")

    def test_credential_and_service_errors_share_common_base(self):
        """Both errors must be reachable through a single ``LDAPError`` catch
        without one being a subclass of the other."""
        assert isinstance(LDAPInvalidCredentialsError("x"), LDAPError)
        assert isinstance(LDAPServiceUnavailableError("x"), LDAPError)
        assert not issubclass(LDAPInvalidCredentialsError, LDAPServiceUnavailableError)
        assert not issubclass(LDAPServiceUnavailableError, LDAPInvalidCredentialsError)

    def test_ldap_error_messages_are_preserved(self):
        cred = LDAPInvalidCredentialsError("Invalid credentials")
        svc = LDAPServiceUnavailableError("LDAP service unavailable")
        assert str(cred) == "Invalid credentials"
        assert str(svc) == "LDAP service unavailable"


# ===========================================================================
# 2. _decode_first — the new safe-indexing helper, exercised directly.
#
# This guards the IndexError that ``attrs.get(key, [b""])[0]`` raised when an
# attribute was present-but-empty. Exercising it directly pins the exact
# contract rather than relying on the integration path.
# ===========================================================================
class TestDecodeFirst:
    def test_missing_key_returns_empty_string(self):
        assert _decode_first({"uid": [b"x"]}, "mail") == ""

    def test_empty_value_list_returns_empty_string(self):
        assert _decode_first({"uid": []}, "uid") == ""

    def test_single_value_decoded(self):
        assert _decode_first({"uid": [b"alice"]}, "uid") == "alice"

    def test_first_of_multiple_values_returned(self):
        assert _decode_first({"uid": [b"alice", b"bob"]}, "uid") == "alice"

    def test_decodes_arbitrary_bytes(self):
        assert _decode_first({"cn": [b"T\xc3\xa9st"]}, "cn") == "Tést"

    def test_empty_bytes_value_returns_empty_string(self):
        assert _decode_first({"mail": [b""]}, "mail") == ""

    def test_does_not_mutate_input(self):
        attrs = {"uid": [b"alice"]}
        _decode_first(attrs, "uid")
        assert attrs == {"uid": [b"alice"]}


# ===========================================================================
# 3. _map_ldap_groups_to_role — the extracted role-mapping method.
# ===========================================================================
class TestMapLdapGroupsToRole:
    def test_matching_admin_group(self, ldap_provider, mock_settings):
        role = ldap_provider._map_ldap_groups_to_role(
            {"memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"]}
        )
        assert role == "admin"

    def test_matching_developer_group(self, ldap_provider, mock_settings):
        role = ldap_provider._map_ldap_groups_to_role(
            {"memberOf": [b"cn=developers,ou=groups,dc=example,dc=com"]}
        )
        assert role == "developer"

    def test_non_matching_group_defaults_to_user(self, ldap_provider, mock_settings):
        role = ldap_provider._map_ldap_groups_to_role(
            {"memberOf": [b"cn=contractors,ou=groups,dc=example,dc=com"]}
        )
        assert role == "user"

    def test_no_member_of_attribute_defaults_to_user(self, ldap_provider, mock_settings):
        role = ldap_provider._map_ldap_groups_to_role({})
        assert role == "user"

    def test_empty_member_of_defaults_to_user(self, ldap_provider, mock_settings):
        role = ldap_provider._map_ldap_groups_to_role({"memberOf": []})
        assert role == "user"

    def test_multiple_groups_pick_highest_role(self, ldap_provider, mock_settings):
        # developer (priority 4) + admin (priority 6) -> admin
        role = ldap_provider._map_ldap_groups_to_role(
            {
                "memberOf": [
                    b"cn=developers,ou=groups,dc=example,dc=com",
                    b"cn=admins,ou=groups,dc=example,dc=com",
                ]
            }
        )
        assert role == "admin"

    def test_role_mapping_is_substring_match(self, ldap_provider, mock_settings):
        """The mapping matches the configured group DN as a *substring* of the
        directory group DN, so a nested-group DN still resolves."""
        role = ldap_provider._map_ldap_groups_to_role(
            {"memberOf": [b"ou=active,cn=admins,ou=groups,dc=example,dc=com"]}
        )
        assert role == "admin"

    def test_no_role_mapping_configured_defaults_to_user(self, ldap_provider, monkeypatch):
        s = Settings(
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping="",
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)
        role = ldap_provider._map_ldap_groups_to_role(
            {"memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"]}
        )
        assert role == "user"


# ===========================================================================
# 4. _resolve_user — the extracted user-resolution method, including the
#    subtle "role unchanged -> no flush" branch that the integration suite
#    does not cover.
# ===========================================================================
class TestResolveUser:
    async def test_existing_user_role_unchanged_does_not_flush(self, ldap_provider, mock_settings):
        """When an existing LDAP user's role already matches the mapped role,
        the provider must NOT issue an extra flush."""
        from engine.db.models import User

        existing = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result

        resolved = await ldap_provider._resolve_user(
            mock_db, "testuser", "testuser@example.com", "Test User", "user"
        )

        assert resolved is existing
        # No role change -> no flush call.
        mock_db.flush.assert_not_called()
        # And no new user was added.
        mock_db.add.assert_not_called()

    async def test_existing_user_role_changed_triggers_flush_and_update(
        self, ldap_provider, mock_settings
    ):
        from engine.db.models import User

        existing = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result

        resolved = await ldap_provider._resolve_user(
            mock_db, "testuser", "testuser@example.com", "Test User", "admin"
        )

        assert resolved is existing
        assert existing.role == "admin"
        mock_db.flush.assert_awaited_once()
        mock_db.add.assert_not_called()

    async def test_email_conflict_returns_none(self, ldap_provider, mock_settings):
        """When the email is already registered under another provider the
        method returns None (the caller reports a conflict)."""
        from engine.db.models import User

        conflict = User(
            email="shared@example.com",
            auth_provider="local",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            # First call: lookup by (provider, external_id) -> not found.
            # Second call: lookup by email -> conflict user.
            r.scalar_one_or_none.return_value = None if call_count == 1 else conflict
            return r

        mock_db.execute = mock_execute

        resolved = await ldap_provider._resolve_user(
            mock_db, "testuser", "shared@example.com", "Test User", "user"
        )
        assert resolved is None
        mock_db.add.assert_not_called()

    async def test_new_user_created(self, ldap_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        mock_db.execute = mock_execute
        added: list[Any] = []

        def track_add(user):
            added.append(user)

        mock_db.add = MagicMock(side_effect=track_add)

        async def mock_refresh(user):
            user.is_active = True

        mock_db.refresh = AsyncMock(side_effect=mock_refresh)

        resolved = await ldap_provider._resolve_user(
            mock_db, "newuser", "new@example.com", "New User", "user"
        )

        assert resolved is not None
        assert len(added) == 1
        assert added[0].email == "new@example.com"
        assert added[0].auth_provider == "ldap"
        assert added[0].external_id == "newuser"
        assert added[0].role == "user"
        mock_db.flush.assert_awaited()
        mock_db.refresh.assert_awaited()


# ===========================================================================
# 5. LDAP-injection prevention — escape_filter_chars MUST be invoked with the
#    raw username, and the escaped value MUST flow into both the bind DN and
#    the search filter. This is the security-critical path.
# ===========================================================================
class TestLDAPInjectionPrevention:
    async def test_escape_filter_chars_invoked_with_raw_username(
        self, ldap_provider, mock_settings
    ):
        conn = _FakeLDAPConn()
        mock_ldap, mock_filter = _recording_ldap_mock(conn)
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await ldap_provider.authenticate(
                username="alice*(|uid=*)", password="pass", db=mock_db
            )

        mock_filter.escape_filter_chars.assert_called_once_with("alice*(|uid=*)")

    async def test_escaped_username_flows_into_bind_dn(self, ldap_provider, mock_settings):
        conn = _FakeLDAPConn()
        mock_ldap, mock_filter = _recording_ldap_mock(conn)
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await ldap_provider.authenticate(username="bob", password="secret", db=mock_db)

        assert conn.bound_with is not None
        bind_dn, password = conn.bound_with
        assert bind_dn == "uid=bob,ou=users,dc=example,dc=com"
        assert password == "secret"

    async def test_escaped_username_flows_into_search_filter(self, ldap_provider, mock_settings):
        conn = _FakeLDAPConn()
        mock_ldap, mock_filter = _recording_ldap_mock(conn)
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await ldap_provider.authenticate(username="carol", password="pass", db=mock_db)

        assert conn.search_args is not None
        _base, _scope, filterstr, attrlist = conn.search_args
        assert filterstr == "(uid=carol)"
        assert attrlist == ["uid", "mail", "cn", "memberOf"]

    async def test_special_characters_are_escaped_before_use(self, ldap_provider, mock_settings):
        """When escape_filter_chars transforms the input, the transformed
        value must be the one used downstream (not the raw input)."""
        conn = _FakeLDAPConn()
        mock_ldap, mock_filter = _recording_ldap_mock(conn)
        # Simulate python-ldap actually escaping the dangerous characters.
        mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x.replace("*", r"\2a"))
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await ldap_provider.authenticate(username="evil*", password="pass", db=mock_db)

        assert conn.bound_with is not None
        bind_dn, _ = conn.bound_with
        # The '*' must be escaped in the constructed DN, proving the escaped
        # value (not the raw username) reached the bind.
        assert "*" not in bind_dn
        assert r"\2a" in bind_dn
        assert conn.search_args is not None
        _base, _scope, filterstr, _attrlist = conn.search_args
        assert "*" not in filterstr


# ===========================================================================
# 6. ImportError-vs-Exception classification boundary — the refactor split a
#    single broad ``except Exception`` into a dedicated ``except ImportError``
#    branch. Pin that an ImportError is classified as service-unavailable and
#    is NOT swallowed as a credential error.
# ===========================================================================
class TestImportErrorClassification:
    async def test_import_error_not_caught_by_credential_branch(
        self, ldap_provider, mock_settings
    ):
        """ImportError must surface as LDAPServiceUnavailableError and must
        never be misclassified as a credential rejection."""
        mock_db = AsyncMock(spec=AsyncSession)
        with (
            patch.dict("sys.modules", {"ldap": None, "ldap.filter": None}),
            pytest.raises(LDAPServiceUnavailableError) as exc_info,
        ):
            await ldap_provider.authenticate(username="u", password="p", db=mock_db)
        assert not isinstance(exc_info.value, LDAPInvalidCredentialsError)
        assert isinstance(exc_info.value, LDAPError)
