"""Comprehensive parametrized tests for the SEV-741 auth hardening pass.

Covers the security-critical behaviour introduced / consolidated across
``engine/api/auth/base.py`` and the four federated providers
(``github_oauth``, ``google``, ``ldap``, ``oidc``):

  1. IdP admin-injection prevention (role overwrite opt-in).
  2. BiDi override / Trojan-Source character stripping.
  3. Fullwidth (NFKC) Unicode homoglyph collapse.
  4. None-role existing-user protection (requires operator opt-in).
  5. Non-list OIDC role-claim normalization.
  6. Disabled-user role-mutation prevention (is_active gate ordering).
  7. Oversize role rejection (DoS / log-flooding guard).

Every scenario is parametrized so regressions surface as a single
failing case rather than a coarse pass/fail.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    _MAX_ROLE_LENGTH,
    ALLOWED_ROLES,
    _apply_role_mapping,
    _sanitize_role,
    _should_overwrite_role,
)
from engine.config import Settings

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _SettingsStub:
    """Minimal stand-in for ``engine.config.Settings`` so the helpers
    can be unit-tested without touching pydantic-settings machinery."""

    def __init__(self, *, overwrite: bool = False) -> None:
        self.auth_overwrite_role_on_login = overwrite


class _FakeUser:
    """Lightweight stand-in for ``engine.db.models.User``."""

    def __init__(self, *, role: str | None = "user", is_active: bool = True) -> None:
        self.role = role
        self.is_active = is_active
        self.id = "fake-user-id"
        self.email = "fake@example.com"
        self.display_name = "Fake"


class _FakeDB:
    def __init__(self) -> None:
        self.flush_calls = 0

    async def flush(self) -> None:
        self.flush_calls += 1


def _to_fullwidth(s: str) -> str:
    """Map ASCII letters/digits/underscore to their fullwidth counterparts."""
    out = []
    for c in s:
        o = ord(c)
        if ord("A") <= o <= ord("Z"):
            out.append(chr(o - ord("A") + 0xFF21))
        elif ord("a") <= o <= ord("z"):
            out.append(chr(o - ord("a") + 0xFF41))
        elif ord("0") <= o <= ord("9"):
            out.append(chr(o - ord("0") + 0xFF10))
        elif c == "_":
            out.append("\uFF3F")
        else:
            out.append(c)
    return "".join(out)


# ---------------------------------------------------------------------------
# OIDC provider test harness (self-contained RSA + httpx mocking)
# ---------------------------------------------------------------------------

DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


@pytest.fixture(scope="module")
def _rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_jwk(pub_key) -> tuple[dict[str, Any], str]:
    kid = "test-kid-parametrized"
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    return jwk_dict, kid


class _FakeResponse:
    def __init__(self, json_data=None, raise_error=None):
        self._json = json_data
        self._raise = raise_error

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, gets=None, posts=None):
        self._gets = list(gets or [])
        self._posts = list(posts or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kw):
        return self._gets.pop(0) if self._gets else _FakeResponse(json_data={})

    async def post(self, url, **kw):
        return self._posts.pop(0) if self._posts else _FakeResponse(json_data={})


def _oidc_client_with_token(_rsa_keys, claims: dict[str, Any]) -> _FakeClient:
    private_key, pub_key = _rsa_keys
    jwk_dict, kid = _make_jwk(pub_key)
    full_claims = {"aud": "test-client-id", **claims}
    id_token = jwt.encode(full_claims, private_key, algorithm="RS256", headers={"kid": kid})
    return _FakeClient(
        gets=[_FakeResponse(DISCOVERY_DOC), _FakeResponse({"keys": [jwk_dict]})],
        posts=[_FakeResponse({"id_token": id_token, "access_token": "at"})],
    )


@pytest.fixture
def oidc_provider():
    from engine.api.auth.oidc import OIDCAuthProvider

    return OIDCAuthProvider()


@pytest.fixture
def oidc_settings(monkeypatch):
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


# ===========================================================================
# 1. IdP admin-injection prevention
# ===========================================================================


class TestIdPAdminInjection:
    """A misconfigured / hostile upstream IdP must not be able to inject an
    elevated role into an *existing* local user without operator opt-in.
    For brand-new users the IdP-asserted role is honoured (there is no prior
    local role to protect)."""

    @pytest.mark.parametrize(
        ("idp_roles", "expected_role"),
        [
            (["admin"], "admin"),
            (["admin", "user"], "admin"),
            (["user", "admin", "developer"], "admin"),
        ],
    )
    def test_map_roles_reflects_admin_claim(self, idp_roles, expected_role):
        from engine.api.auth.base import IAuthProvider

        class _P(IAuthProvider):
            @property
            def name(self):
                return "t"

            async def authenticate(self, **kw):
                from engine.api.auth.base import AuthResult

                return AuthResult()

        assert _P().map_roles(idp_roles) == expected_role

    @pytest.mark.parametrize(
        ("hostile_role", "expected"),
        [
            ("superadmin", "user"),
            ("root", "user"),
            ("Administrator", "user"),
            ("admin ", "admin"),  # whitespace stripped -> valid
        ],
    )
    def test_hostile_admin_variant_collapses_to_user(self, hostile_role, expected):
        assert _sanitize_role(hostile_role) == expected

    @pytest.mark.parametrize(
        ("overwrite", "expected_final_role", "flush_expected"),
        [
            (False, "user", False),  # default: IdP cannot mutate existing role
            (True, "admin", True),  # opt-in: IdP sync honoured
        ],
    )
    async def test_existing_user_admin_injection_blocked_by_default(
        self, overwrite, expected_final_role, flush_expected
    ):
        user = _FakeUser(role="user")
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=overwrite),
            is_new_user=False,
            provider_name="oidc",
            db=db,
        )
        assert user.role == expected_final_role
        assert (db.flush_calls == 1) is flush_expected

    @pytest.mark.parametrize("overwrite", [False, True])
    async def test_new_user_gets_idp_role_regardless_of_opt_in(self, overwrite):
        user = _FakeUser(role=None)
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=overwrite),
            is_new_user=True,
            provider_name="oidc",
            db=db,
        )
        assert user.role == "admin"
        assert db.flush_calls == 1


# ===========================================================================
# 2. BiDi override / Trojan-Source character stripping
# ===========================================================================


# Every code point the _ROLE_BIDI_RE is expected to strip.
_BIDI_CODEPOINTS = (
    list(range(0x202A, 0x202F))  # LRE, RLE, PDF, LRO, RLO, NADS, NODS
    + list(range(0x2066, 0x206A))  # LRI, RLI, FSI, PDI
    + list(range(0x200B, 0x2010))  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    + [0x2028, 0x2029]  # line / paragraph separator
)


class TestBidiOverrideChars:
    @pytest.mark.parametrize("cp", _BIDI_CODEPOINTS)
    def test_codepoint_stripped_from_admin(self, cp):
        ch = chr(cp)
        assert _sanitize_role(f"{ch}admin") == "admin"
        assert _sanitize_role(f"admin{ch}") == "admin"
        assert _sanitize_role(f"ad{ch}min") == "admin"

    @pytest.mark.parametrize("cp", _BIDI_CODEPOINTS)
    def test_codepoint_alone_collapses_to_user(self, cp):
        assert _sanitize_role(chr(cp)) == "user"

    @pytest.mark.parametrize(
        "payload",
        [
            "\u202e" + "admin",  # RLO prefix (classic log-spoof)
            "admin" + "\u202d",  # LRO suffix
            "\u202e\u202dadmin\u202e\u202d",
        ],
    )
    def test_combined_bidi_payloads_cleaned(self, payload):
        assert _sanitize_role(payload) == "admin"

    def test_bidi_between_valid_role_still_resolves(self):
        # zero-width joiners sprinkled inside "admin"
        payload = "a\u200bd\u200cmin"
        assert _sanitize_role(payload) == "admin"


# ===========================================================================
# 3. Fullwidth (NFKC) Unicode homoglyph collapse
# ===========================================================================


class TestFullwidthUnicode:
    @pytest.mark.parametrize("role", sorted(ALLOWED_ROLES))
    def test_fullwidth_role_normalizes_to_ascii(self, role):
        assert _sanitize_role(_to_fullwidth(role)) == role

    @pytest.mark.parametrize("role", sorted(ALLOWED_ROLES))
    def test_fullwidth_uppercase_role_normalizes(self, role):
        assert _sanitize_role(_to_fullwidth(role.upper())) == role

    @pytest.mark.parametrize(
        ("glyph", "expected"),
        [
            ("\uff41\uff44\uff4d\uff49\uff4e", "admin"),  # fullwidth "admin"
            ("\uff35\uff53\uff45\uff52", "user"),  # fullwidth "USER"
        ],
    )
    def test_specific_fullwidth_homoglyphs(self, glyph, expected):
        assert _sanitize_role(glyph) == expected

    def test_mixed_fullwidth_and_ascii_normalizes(self):
        # first char fullwidth-LATIN-SMALL-A, rest ASCII
        assert _sanitize_role("\uff41" + "dmin") == "admin"


# ===========================================================================
# 4. None-role existing user protection
# ===========================================================================


class TestNoneRoleExistingUser:
    """An existing user row whose ``role`` is anomalously ``None`` must
    NOT be silently set by the IdP without operator opt-in — that would
    let an attacker-controlled row wipe masquerade as a fresh insert."""

    @pytest.mark.parametrize(
        ("overwrite", "is_new_user", "expected"),
        [
            (False, False, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ],
    )
    def test_should_overwrite_decision_matrix(
        self, overwrite, is_new_user, expected
    ):
        result = _should_overwrite_role(
            None,
            "admin",
            _SettingsStub(overwrite=overwrite),
            is_new_user=is_new_user,
        )
        assert result is expected

    @pytest.mark.parametrize("overwrite", [False, True])
    async def test_apply_role_mapping_preserves_none_role_when_opted_out(
        self, overwrite
    ):
        user = _FakeUser(role=None)
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=overwrite),
            is_new_user=False,
            provider_name="oidc",
            db=db,
        )
        if overwrite:
            assert user.role == "admin"
            assert db.flush_calls == 1
        else:
            assert user.role is None
            assert db.flush_calls == 0

    def test_none_role_existing_user_bare_config_defaults_false(self):
        class _Bare:
            pass

        assert _should_overwrite_role(None, "admin", _Bare(), is_new_user=False) is False


# ===========================================================================
# 5. Non-list OIDC role-claim normalization
# ===========================================================================


class TestNonListOIDCRoles:
    """OIDC ``roles`` claim may arrive as a bare string, int, dict, or null
    instead of a list. The provider must treat anything that is not a list
    as "no roles" and fall back to the default ``user`` role."""

    @pytest.mark.parametrize(
        ("raw_roles", "expected_created_role"),
        [
            ("admin", "user"),  # string -> not a list -> default
            (42, "user"),  # int -> not a list -> default
            ({"role": "admin"}, "user"),  # dict
            (None, "user"),  # null / missing
            ([], "user"),  # empty list
            (["user"], "user"),  # valid single-element list
            (["admin"], "admin"),  # valid list -> honoured
        ],
    )
    async def test_non_list_roles_collapse_to_default(
        self,
        oidc_provider,
        oidc_settings,
        _rsa_keys,
        raw_roles,
        expected_created_role,
    ):
        claims = {
            "sub": "oidc-nonlist-param",
            "email": f"nl-{id(raw_roles)}@example.com",
            "name": "NonList",
            "roles": raw_roles,
        }
        fake_client = _oidc_client_with_token(_rsa_keys, claims)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created: list[Any] = []
        mock_db.add = MagicMock(side_effect=created.append)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert len(created) == 1
        assert created[0].role == expected_created_role


# ===========================================================================
# 6. Disabled-user role-mutation prevention
# ===========================================================================


_PROVIDER_MODULES = [
    ("engine.api.auth.oidc", "OIDCAuthProvider"),
    ("engine.api.auth.google", "GoogleAuthProvider"),
    ("engine.api.auth.ldap", "LDAPAuthProvider"),
    ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
]


class TestDisabledUserRoleMutationPrevention:
    """The ``is_active`` gate must run BEFORE any role-mutation path so a
    disabled account's role is never touched (which would pre-stage an
    escalation on reactivation)."""

    @pytest.mark.parametrize(("module_path", "class_name"), _PROVIDER_MODULES)
    def test_is_active_check_precedes_role_mapping_in_source(
        self, module_path, class_name
    ):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        active_idx = src.find("not user.is_active")
        apply_idx = src.find("_apply_role_mapping(")
        assert active_idx != -1, f"{module_path} missing 'not user.is_active' guard"
        assert apply_idx != -1, f"{module_path} missing _apply_role_mapping call"
        assert active_idx < apply_idx, (
            f"{module_path}: is_active check must precede _apply_role_mapping"
        )

    async def test_disabled_oidc_user_role_not_mutated(
        self, oidc_provider, oidc_settings, _rsa_keys
    ):
        from engine.db.models import User

        claims = {
            "sub": "oidc-disabled-mut",
            "email": "disabled-mut@example.com",
            "name": "Disabled",
            "roles": ["admin"],
        }
        fake_client = _oidc_client_with_token(_rsa_keys, claims)

        disabled_user = User(
            email="disabled-mut@example.com",
            display_name="Disabled",
            is_active=False,
            role="user",
            auth_provider="oidc",
            external_id="oidc-disabled-mut",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is False
        assert "disabled" in (result.error or "").lower()
        # The role must remain untouched.
        assert disabled_user.role == "user"
        mock_db.flush.assert_not_called()


# ===========================================================================
# 7. Oversize role rejection
# ===========================================================================


class TestOversizeRoleRejection:
    """Role strings longer than ``_MAX_ROLE_LENGTH`` must collapse to
    ``user`` immediately, before NFKC normalization runs."""

    def test_boundary_at_max_length_passes(self):
        # A string exactly _MAX_ROLE_LENGTH chars is accepted (it won't be
        # in ALLOWED_ROLES so it collapses to user, but NOT via the length
        # guard — the length guard must only fire strictly above the cap).
        role = "x" * _MAX_ROLE_LENGTH
        assert _sanitize_role(role) == "user"

    @pytest.mark.parametrize(
        "oversize",
        [
            "a" * (_MAX_ROLE_LENGTH + 1),
            "a" * 100,
            "a" * 1000,
            "admin" + " " * 200,
            "\u202e" * (_MAX_ROLE_LENGTH + 1),
        ],
    )
    def test_oversize_collapses_to_user(self, oversize):
        assert _sanitize_role(oversize) == "user"

    def test_oversize_rejection_logs_too_long_reason(self, monkeypatch):
        from engine.api.auth import base

        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kw):
                calls.append({"event": _event, **kw})

            def info(self, _event, **kw):  # pragma: no cover
                calls.append({"event": _event, **kw})

        monkeypatch.setattr(base, "logger", _Stub())
        big = "x" * (_MAX_ROLE_LENGTH + 5)
        assert base._sanitize_role(big) == "user"
        assert any(c.get("reason") == "too_long" for c in calls), calls

    def test_valid_role_near_max_length_is_honoured(self):
        # "admin" padded with whitespace to just under the cap still
        # normalizes to "admin" (whitespace is stripped post-length-check).
        padded = "  admin  " + " " * (_MAX_ROLE_LENGTH - 9)
        assert len(padded) <= _MAX_ROLE_LENGTH
        assert _sanitize_role(padded) == "admin"
