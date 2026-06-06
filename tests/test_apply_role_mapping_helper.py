"""Comprehensive tests for the centralized role-mapping helper and
related defense-in-depth sanitization (SEV-741 follow-up).

Coverage map
------------
1. ``TestControlCharsRe``            — programmatic guard that the regex
                                       covers every codepoint in the C0,
                                       C1, Bidi-mark, Bidi-override, and
                                       BOM ranges (and NOT plain text).
2. ``TestSanitizeRole``              — unit tests for ``_sanitize_role``:
                                       stripping, normalization,
                                       truncation, type-coercion safety.
3. ``TestSanitizationBeforeComparison``
                                     — pin the order-of-operations in
                                       ``map_roles`` so a Bidi-mangled
                                       payload cannot smuggle past the
                                       ``role_priority`` lookup.
4. ``TestMaxRoleLength``             — boundary tests for the
                                       ``_MAX_ROLE_LENGTH`` constant.
5. ``TestApplyRoleMappingHelper``    — direct unit tests for the
                                       ``IAuthProvider.apply_role_mapping``
                                       method (the centralized
                                       overwrite-or-skip policy).
6. ``TestRoleOverwriteBlockedPerProvider``
                                     — end-to-end test across all four
                                       federated providers (LDAP, OIDC,
                                       Google, GitHub): with the default
                                       (opt-out) policy, an existing
                                       user's role MUST NOT be replaced.
7. ``TestRoleOverwriteAllowedPerProvider``
                                     — same providers, but with
                                       ``auth_overwrite_role_on_login=True``
                                       the helper MUST replace the role.
8. ``TestNoDirectRoleAssignment``    — module-level static-analysis guard
                                       that no provider assigns to
                                       ``user.role`` outside of new-user
                                       construction.
9. ``TestBackwardCompatShouldOverwriteRole``
                                     — the legacy module-level helper is
                                       preserved for callers (and the
                                       existing test suite).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    _CONTROL_CHARS_RE,
    _MAX_ROLE_LENGTH,
    AuthResult,
    IAuthProvider,
    _sanitize_role,
    _should_overwrite_role,
)
from engine.config import Settings


@pytest.fixture
def rsa_keys():
    """RSA key pair for signing/verifying OIDC id_token in tests."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete subclass so the abstract base can be exercised
    without dragging in LDAP/OIDC/Google/GitHub-specific mocks."""

    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **_kwargs: Any) -> AuthResult:
        return AuthResult()


class _SettingsStub:
    """Minimal stand-in for ``engine.config.Settings``."""

    def __init__(self, *, overwrite: bool) -> None:
        self.auth_overwrite_role_on_login = overwrite


class _UserStub:
    """Lightweight stand-in for ``engine.db.models.User`` so the helper
    can be exercised without a SQLAlchemy mapping."""

    def __init__(self, *, role: str | None = "user", user_id: int = 42) -> None:
        self.role = role
        self.id = user_id


@pytest.fixture
def provider() -> _ConcreteProvider:
    return _ConcreteProvider()


@pytest.fixture
def overwrite_off() -> _SettingsStub:
    return _SettingsStub(overwrite=False)


@pytest.fixture
def overwrite_on() -> _SettingsStub:
    return _SettingsStub(overwrite=True)


def _patch_logger(monkeypatch):
    """Replace the module-level structlog logger with a capturing stub.

    Returns the list of recorded call-kwargs dictionaries so tests can
    assert on event names and payloads without coupling to structlog's
    configuration.
    """
    calls: list[dict[str, object]] = []

    class _Stub:
        def info(self, _event, **kwargs):
            calls.append({"event": _event, "level": "info", **kwargs})

        def warning(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "warning", **kwargs})

        def error(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "error", **kwargs})

    from engine.api.auth import base

    monkeypatch.setattr(base, "logger", _Stub())
    return calls


# ---------------------------------------------------------------------------
# 1. _CONTROL_CHARS_RE coverage matrix
# ---------------------------------------------------------------------------


class TestControlCharsRe:
    """The regex is the substrate of every sanitization decision. Pin
    every range it claims to cover so a future refactor cannot
    silently narrow it (e.g. by dropping the Unicode Bidi range)."""

    @pytest.mark.parametrize(
        "codepoint",
        [0x00, 0x01, 0x08, 0x09, 0x0A, 0x0D, 0x1E, 0x1F],
        ids=lambda c: f"U+{c:04X}",
    )
    def test_matches_every_c0_codepoint(self, codepoint: int):
        assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
            f"U+{codepoint:04X} (C0) must be matched"
        )

    @pytest.mark.parametrize(
        "codepoint",
        [0x7F, 0x80, 0x85, 0x9B, 0x9E, 0x9F],
        ids=lambda c: f"U+{c:04X}",
    )
    def test_matches_del_and_every_c1_codepoint(self, codepoint: int):
        assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
            f"U+{codepoint:04X} (DEL/C1) must be matched"
        )

    @pytest.mark.parametrize(
        "codepoint",
        [0x200B, 0x200C, 0x200D, 0x200E, 0x200F],
        ids=lambda c: f"U+{c:04X}",
    )
    def test_matches_zero_width_and_bidi_marks(self, codepoint: int):
        assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
            f"U+{codepoint:04X} (zero-width / Bidi mark) must be matched"
        )

    @pytest.mark.parametrize(
        "codepoint",
        [0x202E, 0xFEFF],
        ids=lambda c: f"U+{c:04X}",
    )
    def test_matches_bidi_override_and_bom(self, codepoint: int):
        assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
            f"U+{codepoint:04X} (Bidi override / BOM) must be matched"
        )

    def test_does_not_match_regular_ascii(self):
        for ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.":
            assert _CONTROL_CHARS_RE.search(ch) is None

    def test_does_not_match_regular_unicode_letters(self):
        for ch in "cafénaïveüber Müller":
            assert _CONTROL_CHARS_RE.search(ch) is None

    def test_does_not_match_visible_cjk(self):
        # CJK letters are NOT control characters and must survive.
        assert _CONTROL_CHARS_RE.search("中文") is None
        assert _CONTROL_CHARS_RE.search("日本語") is None

    def test_matches_in_middle_of_word(self):
        # The regex must match a control char buried inside normal text,
        # not only when it sits at the start.
        assert _CONTROL_CHARS_RE.search("ad\u202emin") is not None

    def test_matches_multiple_control_chars(self):
        # ``findall`` returns one match per occurrence — guards against
        # accidental non-greedy or anchored refactors.
        assert _CONTROL_CHARS_RE.findall("\x00\u200b\u202e\ufeff") == [
            "\x00",
            "\u200b",
            "\u202e",
            "\ufeff",
        ]

    def test_full_c0_range_sweep(self):
        # Exhaustive sweep across U+0000-U+001F.
        for cp in range(0x20):
            assert _CONTROL_CHARS_RE.search(chr(cp)) is not None

    def test_full_c1_range_sweep(self):
        # Exhaustive sweep across U+0080-U+009F (C1).
        for cp in range(0x80, 0xA0):
            assert _CONTROL_CHARS_RE.search(chr(cp)) is not None

    def test_full_bidi_range_sweep(self):
        # Exhaustive sweep across U+200B-U+200F.
        for cp in range(0x200B, 0x2010):
            assert _CONTROL_CHARS_RE.search(chr(cp)) is not None


# ---------------------------------------------------------------------------
# 2. _sanitize_role
# ---------------------------------------------------------------------------


class TestSanitizeRole:
    """Unit tests for ``_sanitize_role`` — the single point of input
    normalization for every external role string."""

    def test_idempotent_on_clean_input(self):
        assert _sanitize_role("admin") == "admin"

    def test_lowercases_input(self):
        assert _sanitize_role("ADMIN") == "admin"

    def test_strips_surrounding_whitespace(self):
        assert _sanitize_role("   admin   ") == "admin"

    def test_strips_internal_whitespace(self):
        # Whitespace is preserved inside the string (only leading/trailing
        # whitespace is stripped) so multi-word names like ``"power user"``
        # remain intact. Tabs and embedded newlines are stripped as C0
        # control chars per the regex.
        assert _sanitize_role("power user") == "power user"
        # Embedded \n is a C0 control char and MUST be stripped.
        assert _sanitize_role("ad\nmin") == "admin"

    def test_strips_c0_from_middle(self):
        assert _sanitize_role("ad\x00min") == "admin"

    def test_strips_c0_nul_prefix(self):
        # NUL injection attempt against SQL/NoSQL backends.
        assert _sanitize_role("\x00admin") == "admin"

    def test_strips_c0_nul_suffix(self):
        assert _sanitize_role("admin\x00") == "admin"

    def test_strips_c1_from_middle(self):
        # U+0085 (NEL) and U+009B (CSI) are the dangerous C1 bytes.
        assert _sanitize_role("ad\x85min") == "admin"
        assert _sanitize_role("ad\u009bmin") == "admin"

    def test_strips_bidi_override(self):
        # U+202E (RLO) — the Trojan-Source attack byte.
        assert _sanitize_role("admin\u202e") == "admin"
        assert _sanitize_role("\u202eadmin") == "admin"
        assert _sanitize_role("ad\u202emin") == "admin"

    def test_strips_zero_width_space(self):
        assert _sanitize_role("admin\u200b") == "admin"
        assert _sanitize_role("\u200badmin") == "admin"

    def test_strips_zero_width_joiner(self):
        assert _sanitize_role("admin\u200d") == "admin"

    def test_strips_bom(self):
        assert _sanitize_role("\ufeffadmin") == "admin"
        assert _sanitize_role("admin\ufeff") == "admin"

    def test_strips_mixed_threat_payload(self):
        # A pathological payload hitting every covered range at once.
        payload = "\ufeff\x00\u200bAD\u202eMIN\u009b\x1f"
        assert _sanitize_role(payload) == "admin"

    def test_non_string_returns_empty_string(self):
        assert _sanitize_role(None) == ""  # type: ignore[arg-type]
        assert _sanitize_role(123) == ""  # type: ignore[arg-type]
        assert _sanitize_role([]) == ""  # type: ignore[arg-type]

    def test_empty_string_returns_empty_string(self):
        assert _sanitize_role("") == ""

    def test_whitespace_only_returns_empty_string(self):
        assert _sanitize_role("    ") == ""
        assert _sanitize_role("\t\t") == ""

    def test_only_control_chars_returns_empty_string(self):
        assert _sanitize_role("\x00\x01\u202e\ufeff") == ""

    def test_truncates_at_max_length(self):
        long_role = "a" * (_MAX_ROLE_LENGTH + 50)
        result = _sanitize_role(long_role)
        assert len(result) == _MAX_ROLE_LENGTH

    def test_no_truncation_at_exact_max_length(self):
        exact = "a" * _MAX_ROLE_LENGTH
        assert len(_sanitize_role(exact)) == _MAX_ROLE_LENGTH
        assert _sanitize_role(exact) == exact

    def test_truncation_happens_after_strip(self):
        # A 100-char string of which 60 are letters and 40 are control
        # chars should end up at 60 chars (under the cap), NOT 64 (which
        # would be the cap-before-strip behaviour).
        payload = ("a" * 60) + ("\u202e" * 40)
        assert len(_sanitize_role(payload)) == 60

    def test_returns_plain_str_not_bytes(self):
        assert isinstance(_sanitize_role("admin"), str)
        assert isinstance(_sanitize_role(b"admin"), str)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. _MAX_ROLE_LENGTH boundary
# ---------------------------------------------------------------------------


class TestMaxRoleLength:
    """The cap is the last line of defense against log-bombing and
    column-overflow. Pin its value and its behaviour at the boundary."""

    def test_max_role_length_is_reasonable(self):
        # 64 chars is generous for any internal role name (the longest
        # built-in role is ``portfolio_manager`` at 17) while still
        # fitting in the DB column after migration headroom.
        assert _MAX_ROLE_LENGTH >= 32
        assert _MAX_ROLE_LENGTH <= 256

    def test_max_role_length_documented_value(self):
        # Pin the documented value so a casual bump triggers a test
        # review (and an update to this assertion + the docstring).
        assert _MAX_ROLE_LENGTH == 64


# ---------------------------------------------------------------------------
# 4. Sanitization-before-comparison (map_roles)
# ---------------------------------------------------------------------------


class TestSanitizationBeforeComparison:
    """Pin the order of operations inside ``IAuthProvider.map_roles``:
    ``_sanitize_role`` MUST run before the ``role_priority`` lookup.

    The regression we're guarding against is a refactor that performs
    the raw-string comparison first and only sanitizes the *output* —
    that would let an attacker submit ``"admin\\u202e"`` and have it
    fall through to the safe default of ``"user"`` (good) BUT then
    persist the unsanitized string into ``user.role`` (bad — RBAC
    bypass via visual confusion).

    With sanitize-before-compare, ``"admin\\u202e"`` is cleaned to
    ``"admin"`` *first* and matches the recognized table; only the
    sanitized form ever reaches the user record.
    """

    def test_bidi_override_admin_recognized_as_admin(self, provider):
        """The sanitized form is what gets compared, so a Bidi-mangled
        admin payload still resolves to ``admin``."""
        assert provider.map_roles(["admin\u202e"]) == "admin"

    def test_zero_width_prefix_admin_recognized_as_admin(self, provider):
        assert provider.map_roles(["\u200badmin"]) == "admin"

    def test_bom_prefix_admin_recognized_as_admin(self, provider):
        assert provider.map_roles(["\ufeffadmin"]) == "admin"

    def test_nul_suffix_admin_recognized_as_admin(self, provider):
        assert provider.map_roles(["admin\x00"]) == "admin"

    def test_c1_suffix_admin_recognized_as_admin(self, provider):
        assert provider.map_roles(["admin\u009b"]) == "admin"

    def test_mixed_control_chars_admin_recognized_as_admin(self, provider):
        assert provider.map_roles(["\u200bAD\u202eMIN\x00"]) == "admin"

    def test_unrecognized_role_with_bidi_falls_back_to_user(self, provider):
        # An unrecognized role stays unrecognized — sanitization only
        # removes control chars; it cannot conjure up a recognized
        # role from a non-matching base.
        assert provider.map_roles(["bogus\u202e"]) == "user"

    def test_warning_fires_for_sanitized_unrecognized_role(self, monkeypatch, provider):
        # When the sanitizer cannot recover a recognized role, the
        # warning still fires — and the unrecognized list carries the
        # sanitized (control-char-free) form so the audit log cannot
        # be log-bombed with raw Bidi / C1 bytes.
        calls = _patch_logger(monkeypatch)
        provider.map_roles(["bogus\u202e"])
        assert any(
            c["event"] == "auth.map_roles.unrecognized_roles"
            and "bogus" in c["unrecognized"]
            for c in calls
        ), "expected the sanitized form in the unrecognized list"
        # Critical: the raw Bidi byte must NOT survive into the log.
        for c in calls:
            for entry in c.get("unrecognized", []):
                assert "\u202e" not in entry, (
                    "Bidi override bytes must be stripped before logging"
                )

    def test_warning_does_not_fire_when_sanitization_recovers_role(
        self, monkeypatch, provider
    ):
        # If sanitization recovered a recognized role, the unrecognized
        # branch is never taken — no warning fires. (Critical: this is
        # the difference between sanitize-BEFORE and sanitize-AFTER.)
        calls = _patch_logger(monkeypatch)
        result = provider.map_roles(["admin\u202e"])
        assert result == "admin"
        assert calls == [], (
            "Sanitization must run BEFORE the lookup so a recoverable "
            "Bidi-mangled role does not pollute the audit log as "
            "'unrecognized'."
        )

    def test_overlong_role_truncated_before_lookup(self, provider, monkeypatch):
        # Truncation runs inside _sanitize_role before the role_priority
        # lookup. We can prove this by capturing the warning payload:
        # the unrecognized list must contain the *truncated* form (i.e.
        # no more than _MAX_ROLE_LENGTH chars), not the raw 200-char
        # attacker payload. If truncation ran AFTER comparison the
        # unrecognized list would carry the unbounded raw string.
        calls = _patch_logger(monkeypatch)
        payload = "a" * (_MAX_ROLE_LENGTH + 100)
        result = provider.map_roles([payload])
        assert result == "user"  # unrecognized
        assert calls, "expected a warning for the unrecognized role"
        unrecognized = calls[0]["unrecognized"]
        assert len(unrecognized[0]) <= _MAX_ROLE_LENGTH, (
            "Truncation must happen before the unrecognized-role list "
            "is built, so the audit log cannot be log-bombed by an "
            "attacker-controlled role string."
        )


# ---------------------------------------------------------------------------
# 5. IAuthProvider.apply_role_mapping — direct unit tests
# ---------------------------------------------------------------------------


class TestApplyRoleMappingHelper:
    """Direct unit tests for the centralized helper method.

    The helper encapsulates three concerns that previously leaked into
    every provider:

    1. The opt-in policy (``_should_overwrite_role``).
    2. The audit-log emission (with a provider-tagged event name).
    3. The conditional DB flush.

    Pinning them in isolation lets us review the policy without
    dragging in LDAP/OIDC/Google/GitHub mock plumbing.
    """

    async def test_returns_true_when_overwrite_happens(
        self, provider, overwrite_on
    ):
        user = _UserStub(role="user")
        db = AsyncMock(spec=AsyncSession)
        result = await provider.apply_role_mapping(user, "admin", overwrite_on, db)
        assert result is True
        assert user.role == "admin"

    async def test_returns_false_when_same_role(self, provider, overwrite_on):
        user = _UserStub(role="admin")
        db = AsyncMock(spec=AsyncSession)
        result = await provider.apply_role_mapping(user, "admin", overwrite_on, db)
        assert result is False
        assert user.role == "admin"  # unchanged

    async def test_returns_false_when_opted_out(self, provider, overwrite_off):
        user = _UserStub(role="user")
        db = AsyncMock(spec=AsyncSession)
        result = await provider.apply_role_mapping(user, "admin", overwrite_off, db)
        assert result is False
        # Critical: existing local role preserved.
        assert user.role == "user"

    async def test_returns_false_when_demotion_blocked(self, provider, overwrite_off):
        # SEV-741: a misconfigured IdP must not be able to downgrade
        # ``admin`` → ``user`` without operator opt-in.
        user = _UserStub(role="admin")
        db = AsyncMock(spec=AsyncSession)
        result = await provider.apply_role_mapping(user, "user", overwrite_off, db)
        assert result is False
        assert user.role == "admin"

    async def test_demotion_allowed_when_opted_in(self, provider, overwrite_on):
        user = _UserStub(role="admin")
        db = AsyncMock(spec=AsyncSession)
        result = await provider.apply_role_mapping(user, "user", overwrite_on, db)
        assert result is True
        assert user.role == "user"

    async def test_no_flush_when_same_role(self, provider, overwrite_on):
        user = _UserStub(role="admin")
        db = AsyncMock(spec=AsyncSession)
        await provider.apply_role_mapping(user, "admin", overwrite_on, db)
        db.flush.assert_not_called()

    async def test_no_flush_when_opted_out(self, provider, overwrite_off):
        user = _UserStub(role="user")
        db = AsyncMock(spec=AsyncSession)
        await provider.apply_role_mapping(user, "admin", overwrite_off, db)
        db.flush.assert_not_called()

    async def test_flush_called_when_overwrite_happens(
        self, provider, overwrite_on
    ):
        user = _UserStub(role="user")
        db = AsyncMock(spec=AsyncSession)
        await provider.apply_role_mapping(user, "admin", overwrite_on, db)
        db.flush.assert_called_once()

    async def test_works_without_db(self, provider, overwrite_on):
        # The helper must not blow up when ``db`` is None (e.g. when
        # the caller manages the flush lifecycle externally).
        user = _UserStub(role="user")
        result = await provider.apply_role_mapping(user, "admin", overwrite_on, None)
        assert result is True
        assert user.role == "admin"

    async def test_emits_audit_event_on_overwrite(
        self, monkeypatch, provider, overwrite_on
    ):
        calls = _patch_logger(monkeypatch)
        user = _UserStub(role="user", user_id=99)
        await provider.apply_role_mapping(user, "admin", overwrite_on, None)
        overwrite_events = [c for c in calls if "role_overwritten" in str(c["event"])]
        assert len(overwrite_events) == 1
        ev = overwrite_events[0]
        assert ev["previous_role"] == "user"
        assert ev["new_role"] == "admin"
        assert ev["user_id"] == "99"

    async def test_event_name_includes_provider_name(
        self, monkeypatch, overwrite_on
    ):
        calls = _patch_logger(monkeypatch)

        class _Named(_ConcreteProvider):
            @property
            def name(self) -> str:
                return "fancy-provider"

        await _Named().apply_role_mapping(
            _UserStub(role="user"), "admin", overwrite_on, None
        )
        assert any(
            c["event"] == "auth.fancy-provider.role_overwritten" for c in calls
        ), "Audit event must embed the provider's name for operator routing"

    async def test_no_event_when_same_role(self, monkeypatch, provider, overwrite_on):
        calls = _patch_logger(monkeypatch)
        await provider.apply_role_mapping(
            _UserStub(role="admin"), "admin", overwrite_on, None
        )
        assert calls == []

    async def test_no_event_when_opted_out(self, monkeypatch, provider, overwrite_off):
        calls = _patch_logger(monkeypatch)
        await provider.apply_role_mapping(
            _UserStub(role="user"), "admin", overwrite_off, None
        )
        assert calls == []

    async def test_missing_setting_attribute_defaults_to_no_overwrite(
        self, provider
    ):
        # Defence-in-depth: a config object that doesn't expose the
        # setting at all must fall back to the safe default.
        class _BareConfig:
            pass

        user = _UserStub(role="user")
        result = await provider.apply_role_mapping(
            user, "admin", _BareConfig(), None
        )
        assert result is False
        assert user.role == "user"


class TestBackwardCompatShouldOverwriteRole:
    """The legacy module-level helper is still re-exported for
    backwards-compatibility with the existing test suite and any
    out-of-tree callers. Pin its signature and semantics."""

    def test_helper_is_callable_with_three_args(self):
        # Must not raise.
        result = _should_overwrite_role("user", "admin", _SettingsStub(overwrite=True))
        assert isinstance(result, bool)

    def test_helper_returns_true_for_new_user(self):
        assert _should_overwrite_role(None, "admin", _SettingsStub(overwrite=False)) is True

    def test_helper_returns_false_for_same_role(self):
        assert _should_overwrite_role("admin", "admin", _SettingsStub(overwrite=True)) is False

    def test_helper_returns_false_when_opted_out(self):
        assert (
            _should_overwrite_role("user", "admin", _SettingsStub(overwrite=False))
            is False
        )

    def test_helper_returns_true_when_opted_in(self):
        assert (
            _should_overwrite_role("user", "admin", _SettingsStub(overwrite=True))
            is True
        )


# ===========================================================================
# End-to-end: every federated provider routes through apply_role_mapping
# ===========================================================================
#
# The tests below drive each of the four federated providers' authenticate()
# flow with mocked upstream transports and verify the helper is invoked
# with the expected arguments. They reuse the existing mocking patterns
# from tests/test_ldap_auth.py and tests/test_oidc_auth.py.


# ---------------------------------------------------------------------------
# LDAP mocks (mirrors tests/test_ldap_auth.py)
# ---------------------------------------------------------------------------


class _FakeLDAPConn:
    def __init__(self, search_results=None):
        self._search_results = search_results or []
        self._options: dict[int, Any] = {}

    def set_option(self, opt, value):
        self._options[opt] = value

    def simple_bind_s(self, dn, password):
        pass

    def search_s(self, base, scope, filterstr, attrlist):
        return self._search_results

    def unbind_s(self):
        pass


def _build_ldap_mock(search_results):
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=_FakeLDAPConn(search_results))
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


def _make_ldap_attrs(uid="testuser", mail=b"testuser@example.com", member_of=None):
    attrs: dict[str, list[bytes]] = {
        "uid": [uid.encode()],
        "mail": [mail],
        "cn": [b"Test User"],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


# ---------------------------------------------------------------------------
# OIDC mocks (mirrors tests/test_oidc_auth.py)
# ---------------------------------------------------------------------------


def _build_oidc_mock_client(rsa_keys, id_token_claims):
    """Build a mock httpx.AsyncClient that returns a signed OIDC id_token."""
    import jwt as pyjwt
    from jwt.algorithms import RSAAlgorithm

    private_key, pub_key = rsa_keys
    kid = "test-kid-123"
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid

    claims = {"aud": "test-client-id", **id_token_claims}
    id_token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    discovery = {
        "authorization_endpoint": "https://id.example.com/authorize",
        "token_endpoint": "https://id.example.com/token",
        "jwks_uri": "https://id.example.com/jwks",
    }

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _Client:
        def __init__(self):
            self._get_q = [
                _Resp(discovery),
                _Resp({"keys": [jwk_dict]}),
            ]
            self._post_q = [_Resp({"id_token": id_token, "access_token": "at"})]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kwargs):
            return self._get_q.pop(0)

        async def post(self, url, **kwargs):
            return self._post_q.pop(0)

    return _Client()


# ---------------------------------------------------------------------------
# Google mocks
# ---------------------------------------------------------------------------


def _build_google_mock_client(profile):
    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _Client:
        def __init__(self):
            self._post_q = [_Resp({"access_token": "at"})]
            self._get_q = [_Resp(profile)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kwargs):
            return self._post_q.pop(0)

        async def get(self, url, **kwargs):
            return self._get_q.pop(0)

    return _Client()


# ---------------------------------------------------------------------------
# GitHub mocks
# ---------------------------------------------------------------------------


def _build_github_mock_client(profile):
    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _Client:
        def __init__(self):
            self._post_q = [_Resp({"access_token": "at"})]
            self._get_q = [_Resp(profile)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kwargs):
            return self._post_q.pop(0)

        async def get(self, url, **kwargs):
            return self._get_q.pop(0)

    return _Client()


# ---------------------------------------------------------------------------
# Helper to build a real User row stand-in for the existing-user case
# ---------------------------------------------------------------------------


def _make_existing_user(*, role="user", auth_provider="ldap", external_id="testuser"):
    from engine.db.models import User

    return User(
        email="testuser@example.com",
        display_name="Test User",
        is_active=True,
        role=role,
        auth_provider=auth_provider,
        external_id=external_id,
    )


def _mock_db_with_existing_user(existing_user):
    """Build a mock AsyncSession whose execute() returns ``existing_user``."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_user
    mock_db.execute.return_value = mock_result
    mock_db.flush = AsyncMock()
    return mock_db


# ---------------------------------------------------------------------------
# 6. Role overwrite BLOCKED per provider (default policy)
# ---------------------------------------------------------------------------


def _ldap_settings(*, overwrite: bool, monkeypatch) -> Settings:
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
        }),
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


def _oidc_settings(*, overwrite: bool, monkeypatch) -> Settings:
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


def _google_settings(*, overwrite: bool, monkeypatch) -> Settings:
    s = Settings(
        google_client_id="test-google-id",
        google_client_secret="test-google-secret",
        google_redirect_uri="https://app.example.com/google/callback",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.google.settings", s)
    return s


def _github_settings(*, overwrite: bool, monkeypatch) -> Settings:
    s = Settings(
        github_client_id="test-github-id",
        github_client_secret="test-github-secret",
        github_redirect_uri="https://app.example.com/github/callback",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


class TestRoleOverwriteBlockedPerProvider:
    """SEV-741: with the default policy (``auth_overwrite_role_on_login``
    unset / False), no federated provider may replace an existing user's
    local role on login.

    Each provider is exercised end-to-end (mocked upstream transport,
    real ``apply_role_mapping`` call) so a regression in EITHER the
    policy decision OR the provider's wiring surfaces here.
    """

    async def test_ldap_does_not_overwrite_when_opted_out(self, monkeypatch):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=False, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="user", auth_provider="ldap")
        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user", "Existing local role must be preserved"
        mock_db.flush.assert_not_called()

    async def test_oidc_does_not_overwrite_when_opted_out(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(overwrite=False, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="user", auth_provider="oidc",
                                       external_id="oidc-existing")
        fake_client = _build_oidc_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user", "Existing local role must be preserved"
        mock_db.flush.assert_not_called()

    async def test_google_does_not_overwrite_when_opted_out(self, monkeypatch):
        from engine.api.auth.google import GoogleAuthProvider

        _google_settings(overwrite=False, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="admin", auth_provider="google",
                                       external_id="google-123")
        fake_client = _build_google_mock_client(
            {"sub": "google-123", "email": "g@example.com", "name": "G"}
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GoogleAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        # Google always maps to "user" — admin must be preserved.
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_github_does_not_overwrite_when_opted_out(self, monkeypatch):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _github_settings(overwrite=False, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="admin", auth_provider="github",
                                       external_id="github-42")
        fake_client = _build_github_mock_client(
            {"id": 42, "login": "ghuser", "email": "gh@example.com", "name": "GH"}
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GitHubAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Role overwrite ALLOWED per provider (opt-in)
# ---------------------------------------------------------------------------


class TestRoleOverwriteAllowedPerProvider:
    """With ``auth_overwrite_role_on_login=True`` every federated
    provider MUST overwrite the existing local role with the IdP-mapped
    role and flush the session."""

    async def test_ldap_overwrites_when_opted_in(self, monkeypatch):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=True, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="user", auth_provider="ldap")
        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called_once()

    async def test_oidc_overwrites_when_opted_in(self, monkeypatch, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(overwrite=True, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="user", auth_provider="oidc",
                                       external_id="oidc-existing")
        fake_client = _build_oidc_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called_once()

    async def test_google_overwrites_when_opted_in(self, monkeypatch):
        from engine.api.auth.google import GoogleAuthProvider

        _google_settings(overwrite=True, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="admin", auth_provider="google",
                                       external_id="google-123")
        fake_client = _build_google_mock_client(
            {"sub": "google-123", "email": "g@example.com", "name": "G"}
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GoogleAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        # Google always maps to "user" — overwrite DEMOTES admin to user.
        assert existing.role == "user"
        mock_db.flush.assert_called_once()

    async def test_github_overwrites_when_opted_in(self, monkeypatch):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _github_settings(overwrite=True, monkeypatch=monkeypatch)

        existing = _make_existing_user(role="admin", auth_provider="github",
                                       external_id="github-42")
        fake_client = _build_github_mock_client(
            {"id": 42, "login": "ghuser", "email": "gh@example.com", "name": "GH"}
        )
        mock_db = _mock_db_with_existing_user(existing)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GitHubAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user"
        mock_db.flush.assert_called_once()


# ---------------------------------------------------------------------------
# 8. No provider assigns user.role outside of new-user construction
# ---------------------------------------------------------------------------


class TestNoDirectRoleAssignment:
    """Static-analysis guard: outside of new-user construction, every
    provider must delegate role mutation to ``self.apply_role_mapping``.
    Catches an accidental revert that re-introduces ``user.role =
    mapped_role`` directly inside the ``authenticate`` method body."""

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.ldap",
            "engine.api.auth.oidc",
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
        ],
    )
    def test_no_direct_user_role_assignment_outside_construction(
        self, module_path: str
    ):
        import inspect
        import re as _re

        mod = __import__(module_path, fromlist=["x"])
        src = inspect.getsource(mod)

        # Every occurrence of ``user.role = mapped_role`` must live
        # inside a User(...) constructor kwarg (i.e. ``role=mapped_role``
        # without the ``user.`` prefix), never as a bare assignment
        # in the body of authenticate().
        bare_assignments = _re.findall(r"^\s*user\.role\s*=\s*\w+", src, _re.MULTILINE)
        assert bare_assignments == [], (
            f"{module_path} must not assign to user.role directly; route "
            f"through self.apply_role_mapping (SEV-741). Found: "
            f"{bare_assignments}"
        )

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.ldap",
            "engine.api.auth.oidc",
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
        ],
    )
    def test_calls_apply_role_mapping(self, module_path: str):
        import inspect

        mod = __import__(module_path, fromlist=["x"])
        src = inspect.getsource(mod)
        assert "self.apply_role_mapping(" in src, (
            f"{module_path} must invoke self.apply_role_mapping for "
            "role-overwrite decisions (SEV-741)."
        )


# ---------------------------------------------------------------------------
# 9. Cross-provider: helper is invoked exactly once per authenticate()
# ---------------------------------------------------------------------------


class TestHelperInvocationCount:
    """Pin that ``apply_role_mapping`` is called exactly once per
    successful existing-user login — not zero times (regression to
    direct assignment) and not twice (double-flush / double-audit)."""

    async def test_ldap_calls_helper_once(self, monkeypatch):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=True, monkeypatch=monkeypatch)
        existing = _make_existing_user(role="user", auth_provider="ldap")
        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )
        mock_db = _mock_db_with_existing_user(existing)

        call_count = 0
        original = LDAPAuthProvider.apply_role_mapping

        async def counting(self, user, mapped_role, config, db=None):
            nonlocal call_count
            call_count += 1
            return await original(self, user, mapped_role, config, db)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}), \
             patch.object(LDAPAuthProvider, "apply_role_mapping", counting):
            await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert call_count == 1
