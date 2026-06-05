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
  - bind DN template validation (parsing, placeholder position, escaping)
  - TLS configuration applied globally before ldap.initialize()
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.ldap import (
    LDAPAuthProvider,
    LDAPBindTemplateError,
    _apply_tls_options,
    _render_bind_dn,
    _validate_bind_dn_template,
)
from engine.config import Settings


@pytest.fixture
def ldap_provider():
    """Fresh LDAPAuthProvider per test.

    Validation in ``__init__`` may have been skipped because ldap is
    not importable in the test environment. The first ``authenticate``
    call inside a mock patch context re-validates with the mocked ldap
    module — clearing the cache here ensures that re-validation happens
    on the test's own ``mock_settings`` rather than a stale template.
    """
    provider = LDAPAuthProvider()
    provider._parsed_bind_dn = None
    provider._validated_for = None
    return provider


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
        }),
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
    ):
        self._bind_raises = bind_raises
        self._search_results = search_results or []
        self._search_raises = search_raises
        self._options: dict[int, Any] = {}

    def set_option(self, opt: int, value: Any) -> None:
        self._options[opt] = value

    def simple_bind_s(self, dn: str, password: str) -> None:
        if self._bind_raises:
            raise self._bind_raises

    def search_s(self, base: str, scope: int, filterstr: str, attrlist: list[str]):
        if self._search_raises:
            raise self._search_raises
        return self._search_results

    def unbind_s(self) -> None:
        pass


def _build_ldap_mock(
    bind_raises: Exception | None = None,
    search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
    search_raises: Exception | None = None,
    str2dn_raises: Exception | None = None,
):
    """Build a MagicMock that fakes ``ldap`` + ``ldap.dn`` + ``ldap.filter``.

    The mock supports:

    * ``ldap.initialize`` -> returns a ``_FakeLDAPConn``.
    * ``ldap.set_option(opt, val)`` -> records the call in a dict
      accessible via the returned ``mock_ldap._global_options``.
    * ``ldap.dn.str2dn / dn2str / escape_dn_chars`` -> round-trip the
      DN through the real parser emulation (splits on ``,`` then ``=``).
    """
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(
        return_value=_FakeLDAPConn(
            bind_raises=bind_raises,
            search_results=search_results,
            search_raises=search_raises,
        )
    )
    # Constants used by the provider code.
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.OPT_X_TLS_REQUIRE_CERT = 0x6A
    mock_ldap.OPT_X_TLS_CACERTFILE = 0x6B
    mock_ldap.OPT_X_TLS_DEMAND = 2
    mock_ldap.OPT_X_TLS_NEVER = 0
    mock_ldap.SCOPE_SUBTREE = 2

    # Capture global set_option calls so tests can assert TLS wiring.
    mock_ldap._global_options: dict[int, Any] = {}

    def _set_option(opt: int, value: Any) -> None:
        mock_ldap._global_options[opt] = value

    mock_ldap.set_option = MagicMock(side_effect=_set_option)

    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)

    mock_dn = MagicMock()

    if str2dn_raises is not None:
        mock_dn.str2dn = MagicMock(side_effect=str2dn_raises)
    else:
        def _str2dn(dn: str):
            # Lightweight parser good enough for the canonical templates
            # used in the test suite (``uid=foo,ou=bar,dc=baz``). Returns
            # the same structure as ``ldap.dn.str2dn``.
            if not dn:
                return []
            out: list[list[tuple[str, str, int]]] = []
            for raw_rdn in dn.split(","):
                rdn = raw_rdn.strip()
                if not rdn:
                    continue
                if "=" not in rdn:
                    err = f"bad DN: {dn!r}"
                    raise ValueError(err)
                attr, value = rdn.split("=", 1)
                out.append([(attr.strip(), value.strip(), 1)])
            return out

        mock_dn.str2dn = MagicMock(side_effect=_str2dn)

    def _dn2str(parsed):
        return ",".join(
            "+".join(f"{a}={v}" for a, v, _ in rdn) for rdn in parsed
        )

    mock_dn.dn2str = MagicMock(side_effect=_dn2str)
    mock_dn.escape_dn_chars = MagicMock(
        # Escape characters that have meaning in a DN value. For tests we
        # only need the *behaviour* (e.g. comma -> \\,) rather than the
        # full table.
        side_effect=lambda s: s.replace("\\", "\\\\").replace(",", "\\,")
        .replace("+", "\\+").replace('"', '\\"').replace("<", "\\<")
        .replace(">", "\\>").replace(";", "\\;").replace("=", "\\="),
    )

    # Wire mock_dn onto mock_ldap so ``ldap.dn`` (looked up as an
    # attribute of the ldap package) resolves to the same object as the
    # ``ldap.dn`` entry we patched into sys.modules. Without this,
    # MagicMock auto-generates an unrelated ``mock_ldap.dn`` child which
    # shadows the explicit sys.modules patch.
    mock_ldap.dn = mock_dn

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
    async def test_bind_failure_returns_invalid_credentials(
        self, ldap_provider, mock_settings
    ):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            bind_raises=Exception("LDAP bind failed")
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
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
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert "Invalid credentials" in result.error


class TestLDAPAuthenticateSearchResults:
    async def test_empty_results_user_not_found(self, ldap_provider, mock_settings):
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(search_results=[])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
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
    async def test_new_user_created_with_mapped_role(
        self, ldap_provider, mock_settings
    ):
        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
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

    async def test_new_user_default_role_when_no_groups(
        self, ldap_provider, mock_settings
    ):
        attrs = _make_ldap_attrs(member_of=[])
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=user2,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="user2", password="pass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert "user" in result.user_info.roles

    async def test_developer_role_mapping(self, ldap_provider, mock_settings):
        attrs = _make_ldap_attrs(
            member_of=[b"cn=developers,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=devuser,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="devuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert len(added_users) == 1
        assert added_users[0].role == "developer"

    async def test_multiple_groups_maps_highest_role(
        self, ldap_provider, mock_settings
    ):
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

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="multi", password="pass", db=mock_db
            )

        assert result.success is True
        assert added_users[0].role == "admin"

    async def test_email_fallback_when_mail_empty(
        self, ldap_provider, mock_settings
    ):
        attrs = _make_ldap_attrs(mail=b"")
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=nomail,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _ = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="nomail", password="pass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "nomail@ldap"

    async def test_cn_fallback_when_empty(
        self, ldap_provider, mock_settings
    ):
        attrs = _make_ldap_attrs(cn=b"")
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=nocn,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _ = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="nocn", password="pass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.display_name == "nocn"


class TestLDAPAuthenticateExistingUser:
    async def test_existing_ldap_user_logs_in(
        self, ldap_provider, mock_settings
    ):
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

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "testuser@example.com"
        mock_db.add.assert_not_called()

    async def test_existing_user_role_updated(
        self, ldap_provider, mock_settings
    ):
        from engine.db.models import User

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
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

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()


class TestLDAPAuthenticateEmailConflict:
    async def test_email_registered_with_different_provider(
        self, ldap_provider, mock_settings
    ):
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

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
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

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is False
        assert "disabled" in result.error.lower()


class TestLDAPRoleMappingEmpty:
    async def test_no_role_mapping_configured(
        self, ldap_provider, monkeypatch
    ):
        s = Settings(
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping="",
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=somegroup,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=norolemap,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db, _ = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter}):
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
        result = await ldap_provider.create_user(
            _user_info=MagicMock()
        )
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
# Bind DN template validation
# ---------------------------------------------------------------------------


class TestBindDNTemplateValidation:
    """Verify _validate_bind_dn_template catches malformed templates."""

    @staticmethod
    def _patch_ldap_modules(str2dn_raises: Exception | None = None):
        """Build a sys.modules patch with both ``ldap`` and ``ldap.dn`` wired.

        ``patch.dict("sys.modules", ...)`` doesn't reliably override
        ``ldap.dn`` because Python's import machinery falls back to the
        parent's attribute when the submodule isn't cached — and a bare
        ``MagicMock()`` auto-generates an unrelated ``.dn`` child that
        shadows the explicit sys.modules entry. We avoid this by setting
        ``mock_ldap.dn = mock_dn`` so both paths resolve to the same
        object.
        """
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(str2dn_raises=str2dn_raises)
        return patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter},
        ), mock_ldap, mock_filter, mock_dn

    def test_valid_template_with_placeholder(self):
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher:
            parsed = _validate_bind_dn_template(
                "uid={{username}},ou=users,dc=example,dc=com"
            )
        assert parsed == [
            [("uid", "{{username}}", 1)],
            [("ou", "users", 1)],
            [("dc", "example", 1)],
            [("dc", "com", 1)],
        ]

    def test_valid_template_without_placeholder(self):
        # Service-account bind (no {{username}}) is allowed.
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher:
            parsed = _validate_bind_dn_template(
                "cn=admin,ou=services,dc=example,dc=com"
            )
        assert parsed == [
            [("cn", "admin", 1)],
            [("ou", "services", 1)],
            [("dc", "example", 1)],
            [("dc", "com", 1)],
        ]

    def test_empty_template_returns_empty_list(self):
        # No ldap module interaction required for the empty path.
        assert _validate_bind_dn_template("") == []

    def test_placeholder_in_attribute_position_rejected(self):
        # {{username}} as the attribute type is a security risk: an
        # attacker who controls the username could inject arbitrary
        # attribute types.
        template = "{{username}}=foo,ou=users,dc=example,dc=com"
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher, pytest.raises(LDAPBindTemplateError, match="value position"):
            _validate_bind_dn_template(template)

    def test_malformed_dn_rejected(self):
        # str2dn raises on garbage input.
        template = "this is not a DN at all"
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher, pytest.raises(LDAPBindTemplateError, match="Invalid LDAP bind DN"):
            _validate_bind_dn_template(template)

    def test_str2dn_failure_is_wrapped(self):
        # str2dn_raises path: ensures we translate any python-ldap
        # exception into LDAPBindTemplateError so callers don't need to
        # know the underlying library's exception hierarchy.
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules(
            str2dn_raises=RuntimeError("ldap lib blew up")
        )
        with patcher, pytest.raises(LDAPBindTemplateError):
            _validate_bind_dn_template(
                "uid={{username}},ou=users,dc=example,dc=com"
            )


class TestBindDNRendering:
    """Verify _render_bind_dn escapes the username and reassembles safely."""

    @staticmethod
    def _patch_ldap_modules():
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock()
        return patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter},
        ), mock_ldap, mock_filter, mock_dn

    def test_basic_render(self):
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher:
            parsed = _validate_bind_dn_template(
                "uid={{username}},ou=users,dc=example,dc=com"
            )
            rendered = _render_bind_dn(parsed, "alice")
        assert rendered == "uid=alice,ou=users,dc=example,dc=com"

    def test_username_with_dn_special_chars_escaped(self):
        # A comma in the username would normally break the DN — escape
        # prevents injection of an extra RDN.
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher:
            parsed = _validate_bind_dn_template(
                "uid={{username}},ou=users,dc=example,dc=com"
            )
            rendered = _render_bind_dn(parsed, "alice,bob")
        # Comma in the value is escaped to \,.
        assert rendered == "uid=alice\\,bob,ou=users,dc=example,dc=com"

    def test_empty_parsed_returns_empty_string(self):
        patcher, _mock_ldap, _mock_filter, _mock_dn = self._patch_ldap_modules()
        with patcher:
            assert _render_bind_dn([], "alice") == ""


class TestProviderEagerValidation:
    """Verify LDAPAuthProvider re-validates on authenticate() if settings change."""

    async def test_authenticate_returns_config_error_on_bad_template(
        self, ldap_provider, monkeypatch
    ):
        # Simulate a misconfigured bind DN: placeholder in attribute
        # position. Provider should surface a generic "LDAP configuration
        # error" to callers and log the underlying detail.
        s = Settings(
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="{{username}}=foo,ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter, mock_dn = _build_ldap_mock()
        mock_db = AsyncMock(spec=AsyncSession)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter},
        ):
            result = await ldap_provider.authenticate(
                username="alice", password="pass", db=mock_db
            )

        assert result.success is False
        assert result.error == "LDAP configuration error"
        # No bind should have been attempted on a misconfigured template.
        mock_ldap.initialize.assert_not_called()

    async def test_validation_cached_on_repeated_calls(
        self, ldap_provider, mock_settings
    ):
        # Two calls back-to-back: the second should re-use the parsed
        # template (cache hit, _ensure_template_validated returns early).
        attrs = _make_ldap_attrs()
        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        from engine.db.models import User

        existing_user = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter},
        ):
            first = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )
            str2dn_call_count_after_first = mock_dn.str2dn.call_count
            second = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )
            str2dn_call_count_after_second = mock_dn.str2dn.call_count

        assert first.success is True
        assert second.success is True
        # The second call hit the cache: str2dn not invoked again.
        assert str2dn_call_count_after_second == str2dn_call_count_after_first


# ---------------------------------------------------------------------------
# TLS configuration (module-level ldap.set_option() before initialize())
# ---------------------------------------------------------------------------


class TestTLSConfiguration:
    """Verify TLS options are applied to the *global* ldap module before
    ``ldap.initialize()`` runs — per-connection TLS options are unreliable
    in python-ldap and the globals must be set first."""

    def test_apply_tls_options_demand_by_default(self, monkeypatch):
        # Default: ldap_tls_require_cert=True -> OPT_X_TLS_DEMAND.
        s = Settings(
            ldap_server_url="ldaps://ldap.example.com",
            ldap_ca_cert_file="/etc/ssl/certs/ldap-ca.pem",
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap = MagicMock()
        mock_ldap.OPT_X_TLS_REQUIRE_CERT = 0x6A
        mock_ldap.OPT_X_TLS_CACERTFILE = 0x6B
        mock_ldap.OPT_X_TLS_DEMAND = 2
        mock_ldap.OPT_X_TLS_NEVER = 0

        with patch.dict("sys.modules", {"ldap": mock_ldap}):
            _apply_tls_options()

        # Both options set via *global* ldap.set_option (not
        # conn.set_option — that's the bug class we're hardening
        # against).
        calls = mock_ldap.set_option.call_args_list
        opts_set = {c.args for c in calls}
        assert (mock_ldap.OPT_X_TLS_CACERTFILE, "/etc/ssl/certs/ldap-ca.pem") in opts_set
        assert (mock_ldap.OPT_X_TLS_REQUIRE_CERT, mock_ldap.OPT_X_TLS_DEMAND) in opts_set

    def test_apply_tls_options_never_when_disabled(self, monkeypatch):
        s = Settings(
            ldap_server_url="ldaps://ldap.example.com",
            ldap_tls_require_cert=False,
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap = MagicMock()
        mock_ldap.OPT_X_TLS_REQUIRE_CERT = 0x6A
        mock_ldap.OPT_X_TLS_CACERTFILE = 0x6B
        mock_ldap.OPT_X_TLS_DEMAND = 2
        mock_ldap.OPT_X_TLS_NEVER = 0

        with patch.dict("sys.modules", {"ldap": mock_ldap}):
            _apply_tls_options()

        # No CA file configured — OPT_X_TLS_CACERTFILE should NOT be set.
        ca_calls = [
            c for c in mock_ldap.set_option.call_args_list
            if c.args and c.args[0] == mock_ldap.OPT_X_TLS_CACERTFILE
        ]
        assert ca_calls == []
        # OPT_X_TLS_REQUIRE_CERT set to NEVER.
        require_calls = [
            c for c in mock_ldap.set_option.call_args_list
            if c.args and c.args[0] == mock_ldap.OPT_X_TLS_REQUIRE_CERT
        ]
        assert len(require_calls) == 1
        assert require_calls[0].args[1] == mock_ldap.OPT_X_TLS_NEVER

    async def test_authenticate_applies_tls_before_initialize(  # noqa: PLR0915
        self, ldap_provider, mock_settings
    ):
        # Regression: TLS globals MUST be set before initialize() — the
        # underlying libldap reads them at initialize time.
        attrs = _make_ldap_attrs()
        fake_conn = _FakeLDAPConn(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )

        # Track call order via a shared list — we use standalone lists
        # rather than MagicMock.side_effect stacking because the latter
        # does not compose reliably with the side effects already set up
        # by _build_ldap_mock.
        call_order: list[str] = []
        # Snapshot the TLS-related option codes up front.
        tls_opt_codes: set[int] = set()
        seen_initialize = False

        def _tracking_set_option(opt, value):
            if opt in tls_opt_codes:
                call_order.append(f"set_option:{opt}")
                if seen_initialize:
                    call_order.append("set_option_after_initialize")

        def _tracking_initialize(*_args, **_kwargs):
            nonlocal seen_initialize
            seen_initialize = True
            call_order.append("initialize")
            return fake_conn

        mock_ldap = MagicMock()
        mock_ldap.OPT_NETWORK_TIMEOUT = 7
        mock_ldap.OPT_TIMEOUT = 8
        mock_ldap.OPT_X_TLS_REQUIRE_CERT = 0x6A
        mock_ldap.OPT_X_TLS_CACERTFILE = 0x6B
        mock_ldap.OPT_X_TLS_DEMAND = 2
        mock_ldap.OPT_X_TLS_NEVER = 0
        mock_ldap.SCOPE_SUBTREE = 2
        tls_opt_codes.add(mock_ldap.OPT_X_TLS_REQUIRE_CERT)
        tls_opt_codes.add(mock_ldap.OPT_X_TLS_CACERTFILE)
        mock_ldap.set_option = MagicMock(side_effect=_tracking_set_option)
        mock_ldap.initialize = MagicMock(side_effect=_tracking_initialize)

        mock_filter = MagicMock()
        mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
        mock_dn = MagicMock()

        def _str2dn(dn):
            if not dn:
                return []
            out = []
            for raw_rdn in dn.split(","):
                rdn = raw_rdn.strip()
                if not rdn:
                    continue
                if "=" not in rdn:
                    err = f"bad DN: {dn!r}"
                    raise ValueError(err)
                attr, value = rdn.split("=", 1)
                out.append([(attr.strip(), value.strip(), 1)])
            return out

        mock_dn.str2dn = MagicMock(side_effect=_str2dn)
        mock_dn.dn2str = MagicMock(
            side_effect=lambda parsed: ",".join(
                "+".join(f"{a}={v}" for a, v, _ in rdn) for rdn in parsed
            )
        )
        mock_dn.escape_dn_chars = MagicMock(side_effect=lambda s: s.replace(",", "\\,"))
        mock_ldap.dn = mock_dn

        # _make_mock_db sets up the add() side-effect that toggles
        # is_active=True on newly-created users (the auth flow calls
        # db.add() then refresh()).
        mock_db, _added_users = _make_mock_db()
        mock_db.execute = _mock_execute_factory(None, None)

        # Configure TLS settings so set_option is exercised.
        mock_settings.ldap_ca_cert_file = "/etc/ssl/certs/ldap-ca.pem"

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter},
        ):
            result = await ldap_provider.authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is True, f"unexpected failure: {result.error}"

        # All global set_option calls must come before initialize().
        tls_set_option_count = sum(
            1 for c in call_order if c.startswith("set_option:")
        )
        assert tls_set_option_count >= 2, (
            f"TLS set_option calls should have fired at least twice "
            f"(REQUIRE_CERT + CACERTFILE); got {call_order!r}"
        )
        assert "set_option_after_initialize" not in call_order, (
            f"TLS options must be set before initialize(): {call_order!r}"
        )


# ---------------------------------------------------------------------------
# Integration test: invalid-cert LDAP server fails bind
# ---------------------------------------------------------------------------


class TestInvalidCertBindFailure:
    """Integration-flavoured test: when the LDAP server presents a
    certificate that doesn't validate against the configured CA, the bind
    must fail and the caller must see ``"Invalid credentials"`` (never a
    success and never an uncaught exception)."""

    async def test_tls_handshake_failure_surfaces_as_invalid_credentials(
        self, ldap_provider, mock_settings
    ):
        # ldaps:// URL + TLS_REQUIRE_CERT=DEMAND + an invalid cert -> the
        # mock simulates the bind raising ``ldap.SERVER_DOWN`` (the
        # python-ldap wrapper for TLS handshake failures).
        mock_settings.ldap_server_url = "ldaps://ldap.example.com:636"
        mock_settings.ldap_tls_require_cert = True
        mock_settings.ldap_ca_cert_file = "/etc/ssl/certs/ldap-ca.pem"

        mock_ldap, mock_filter, mock_dn = _build_ldap_mock(
            bind_raises=ConnectionError("TLS handshake failed: certificate verify failed")
        )
        mock_db = AsyncMock(spec=AsyncSession)

        with patch.dict(
            "sys.modules",
            {"ldap": mock_ldap, "ldap.dn": mock_dn, "ldap.filter": mock_filter},
        ):
            result = await ldap_provider.authenticate(
                username="alice", password="pass", db=mock_db
            )

        # The user must NEVER see success on a TLS failure — that would
        # silently downgrade security.
        assert result.success is False
        # Generic error: don't leak TLS internals to the caller.
        assert result.error == "Invalid credentials"

        # TLS options were applied (proves the path is wired).
        tls_calls = [
            c.args for c in mock_ldap.set_option.call_args_list
            if c.args and c.args[0] in (
                mock_ldap.OPT_X_TLS_REQUIRE_CERT,
                mock_ldap.OPT_X_TLS_CACERTFILE,
            )
        ]
        assert tls_calls, "TLS options should have been set before bind"

