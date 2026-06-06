"""Comprehensive tests for the role-claim sanitization hardening
(SEV-741 follow-up: allowlist + NFKC + size cap + None-role opt-in).

Targets
-------

This module pins the behaviour of the recently-changed code paths in:

* ``engine/api/auth/base.py``
    - ``ALLOWED_ROLES`` — closed set of internal roles
    - ``_sanitize_role`` — NFKC normalisation + strict allowlist
      regex ``^[A-Za-z0-9_-]{1,64}$``, **no truncation**
    - ``_should_overwrite_role`` — ``None`` role on an existing user
      is no longer a short-circuit (requires opt-in)

* ``engine/api/auth/oidc.py``
    - ``raw_roles`` claim shape normalisation (``str``/``list``/``dict``)

* ``engine/api/auth/{google,github_oauth,ldap,oidc}.py``
    - ``is_active`` gate is evaluated **before** ``_should_overwrite_role``
      (audit ordering, "disabled user never triggers role-overwrite event")

The existing ``tests/test_auth_role_promotion_security_fix.py`` already
covers the headline behavioural change (no implicit promotion).  This
module focuses on the **sanitisation layer itself** — Unicode spoofing,
bidi override, oversized payloads, injection attempts, and provider
ordering — which were not previously pinned down.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    _ROLE_PATTERN,
    _ROLE_PRIORITY,
    ALLOWED_ROLES,
    _sanitize_role,
    _should_overwrite_role,
)
from engine.config import Settings

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _SettingsStub:
    """Minimal stand-in for ``engine.config.Settings``."""

    def __init__(self, *, overwrite: bool = False) -> None:
        self.auth_overwrite_role_on_login = overwrite


# ---------------------------------------------------------------------------
# 1. ALLOWED_ROLES — closed-set contract
# ---------------------------------------------------------------------------


class TestAllowedRolesContract:
    """The closed set of roles the engine will persist on a ``User``.

    Any change here is a privilege-boundary change and must be
    intentional — pin the type, contents and the *absence* of legacy
    roles.
    """

    def test_is_a_frozenset(self):
        """A ``frozenset`` so it cannot be mutated at runtime."""
        assert isinstance(ALLOWED_ROLES, frozenset)

    def test_contains_exactly_the_documented_roles(self):
        assert frozenset(
            {"user", "viewer", "developer", "portfolio_manager", "admin"}
        ) == ALLOWED_ROLES

    @pytest.mark.parametrize(
        "legacy_role",
        ["retail_trader", "quant_dev", "root", "superuser", "god", "owner"],
    )
    def test_legacy_or_synthetic_roles_are_excluded(self, legacy_role):
        """Roles outside the closed set must collapse to ``user`` —
        pin that they are *not* present."""
        assert legacy_role not in ALLOWED_ROLES

    def test_role_priority_is_consistent_with_allowed_roles(self):
        """``_ROLE_PRIORITY`` must have an entry for every member of
        ``ALLOWED_ROLES`` so the precedence ordering never crashes
        with a KeyError."""
        assert set(_ROLE_PRIORITY.keys()) == ALLOWED_ROLES

    def test_admin_is_highest_priority(self):
        assert _ROLE_PRIORITY["admin"] == max(_ROLE_PRIORITY.values())

    def test_viewer_is_lowest_priority(self):
        assert _ROLE_PRIORITY["viewer"] == min(_ROLE_PRIORITY.values())

    def test_priority_order_is_total(self):
        """No two roles share a priority value — the ordering is total."""
        values = list(_ROLE_PRIORITY.values())
        assert len(set(values)) == len(values)


# ---------------------------------------------------------------------------
# 2. _sanitize_role — type/shape contract
# ---------------------------------------------------------------------------


class TestSanitizeRoleTypeContract:
    """``_sanitize_role`` is called from every provider — it must
    never crash on adversarial input (an IdP claim can be int, dict,
    None, …). Invalid input returns ``None``; ``map_roles`` then
    collapses that to ``user``.
    """

    @pytest.mark.parametrize(
        "value",
        [
            None,
            42,
            3.14,
            True,
            False,
            ["admin"],
            {"role": "admin"},
            object(),
            b"admin",
        ],
    )
    def test_non_string_returns_none(self, value):
        assert _sanitize_role(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("user", "user"),
            ("admin", "admin"),
            ("portfolio_manager", "portfolio_manager"),
            ("developer", "developer"),
            ("viewer", "viewer"),
        ],
    )
    def test_valid_allowed_role_round_trips(self, value, expected):
        assert _sanitize_role(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("ADMIN", "admin"),
            ("Admin", "admin"),
            ("  Admin  ", "admin"),
            ("\tadmin\n", "admin"),
        ],
    )
    def test_case_and_outer_whitespace_normalized(self, value, expected):
        assert _sanitize_role(value) == expected

    def test_empty_string_returns_none(self):
        assert _sanitize_role("") is None

    @pytest.mark.parametrize("value", ["   ", "\t\t", "\n\n", " \t\n "])
    def test_whitespace_only_returns_none(self, value):
        assert _sanitize_role(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            "retail_trader",
            "quant_dev",
            "superuser",
            "root",
            "owner",
            "guest",
        ],
    )
    def test_role_outside_allowed_set_still_sanitizes(self, value):
        """Roles outside ALLOWED_ROLES still pass the allowlist
        regex (they are made of valid ASCII chars) — ``_sanitize_role``
        returns the lower-cased form; ``map_roles`` then collapses
        them to ``user``. This is the contract: the sanitiser is
        *shape-only*, the closed-set check happens in ``map_roles``."""
        assert _sanitize_role(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "user admin",
            "user,admin",
            "user;admin",
            "user|admin",
            "user&admin",
            "user/admin",
            "user\\admin",
            "user(admin)",
            "user[admin]",
            "user{admin}",
            "user<admin>",
            "user!admin",
        ],
    )
    def test_punctuation_and_separators_rejected(self, value):
        assert _sanitize_role(value) is None

    def test_dot_rejected(self):
        """Role names must not contain dots — prevents
        namespace-confusion attacks (``a.b`` vs ``a``)."""
        assert _sanitize_role("user.admin") is None

    def test_at_sign_rejected(self):
        assert _sanitize_role("admin@example") is None

    def test_space_inside_role_rejected(self):
        assert _sanitize_role("port folio_manager") is None

    def test_underscore_and_hyphen_accepted(self):
        """The allowlist explicitly permits ``_`` and ``-`` so legacy
        / domain-style role names still flow through the sanitiser."""
        assert _sanitize_role("portfolio_manager") == "portfolio_manager"
        assert _sanitize_role("role-with-dashes") == "role-with-dashes"

    def test_digits_accepted(self):
        assert _sanitize_role("role123") == "role123"
        assert _sanitize_role("123role") == "123role"

    def test_leading_digit_accepted(self):
        """Unlike a Python identifier, role names may begin with a
        digit — the regex is purely an allowlist."""
        assert _sanitize_role("1st_line") == "1st_line"


# ---------------------------------------------------------------------------
# 3. _sanitize_role — Unicode / NFKC normalisation
# ---------------------------------------------------------------------------


class TestSanitizeRoleNfkcNormalization:
    """``NFKC`` collapses obvious compatibility-form spoofs before the
    allowlist runs, but does **not** rescue inputs whose canonical form
    still contains non-ASCII codepoints."""

    def test_fullwidth_admin_collapses_to_admin(self):
        """The classic Unicode-spoofing payload ``ａｄｍｉｎ``
        (fullwidth Latin) is NFKC-normalised to ``admin`` and accepted."""
        assert _sanitize_role("ａｄｍｉｎ") == "admin"

    def test_fullwidth_uppercase_admin_collapses(self):
        """``ＡＤＭＩＮ`` (fullwidth uppercase) collapses + lowercases."""
        assert _sanitize_role("ＡＤＭＩＮ") == "admin"

    def test_superscript_digit_collapses(self):
        """NFKC folds ``²`` -> ``2``; combined with ASCII letters this
        survives the allowlist."""
        assert _sanitize_role("role²") == "role2"

    def test_combining_diacritic_rejected(self):
        """NFKC does **not** decompose a combining diacritic away.
        ``á`` (decomposable) survives NFKC, fails the ASCII allowlist
        and is rejected."""
        a_with_acute = "adm" + "í" + "n"
        assert _sanitize_role(a_with_acute) is None

    def test_combining_acute_on_ascii_rejected(self):
        """A base ASCII letter carrying a combining mark (e + ́) does
        not collapse under NFKC, so the resulting 2-codepoint grapheme
        fails the allowlist."""
        e_with_combining_acute = "e\u0301"
        assert _sanitize_role(f"admin{e_with_combining_acute}") is None

    def test_cyrillic_homoglyph_rejected(self):
        """A Cyrillic ``а`` (U+0430) is *not* NFKC-equivalent to
        Latin ``a`` — the spoof fails the allowlist and is rejected."""
        # First char is Cyrillic 'a', rest Latin
        spoof = "\u0430" + "dmin"
        assert _sanitize_role(spoof) is None

    def test_greek_homoglyph_rejected(self):
        """Greek ``ο`` (U+03BF) is not NFKC-equivalent to Latin ``o``."""
        spoof = "admine" + "\u03BF"
        assert _sanitize_role(spoof) is None

    def test_bidi_rlo_override_rejected(self):
        """``U+202E`` (RIGHT-TO-LEFT OVERRIDE) is a classic
        visual-spoofing primitive — must be rejected even when
        the rest of the string is ASCII."""
        rlo = chr(0x202E)
        assert _sanitize_role(rlo + "admin") is None

    def test_bidi_lro_override_rejected(self):
        assert _sanitize_role(chr(0x202D) + "admin") is None

    def test_bidi_pdf_rejected(self):
        assert _sanitize_role("admin" + chr(0x202C)) is None

    def test_bidi_lre_rejected(self):
        assert _sanitize_role(chr(0x202A) + "admin") is None

    def test_bidi_rle_rejected(self):
        assert _sanitize_role(chr(0x202B) + "admin") is None

    def test_zero_width_space_rejected(self):
        assert _sanitize_role("admin" + chr(0x200B)) is None

    def test_zero_width_joiner_rejected(self):
        assert _sanitize_role("admin" + chr(0x200D)) is None

    def test_zero_width_non_joiner_rejected(self):
        assert _sanitize_role("admin" + chr(0x200C)) is None

    def test_variation_selector_rejected(self):
        """Variation selectors are invisible codepoints used for
        emoji presentation — must not sneak through the allowlist."""
        assert _sanitize_role("admin" + chr(0xFE00)) is None
        assert _sanitize_role("admin" + chr(0xFE0F)) is None

    def test_soft_hyphen_rejected(self):
        assert _sanitize_role("admin" + chr(0x00AD)) is None

    def test_non_breaking_space_rejected(self):
        """A NBSP (U+00A0) in the middle of a role must not pass —
        ``strip()`` only removes ASCII whitespace."""
        assert _sanitize_role("admin\u00A0user") is None

    def test_nul_byte_rejected(self):
        """NUL injection — some downstream systems (DBs, log parsers)
        treat ``\\0`` as a string terminator. Reject outright."""
        assert _sanitize_role("admin\x00user") is None

    def test_leading_nul_byte_rejected(self):
        assert _sanitize_role("\x00admin") is None

    def test_newline_rejected(self):
        """CRLF injection into audit logs."""
        assert _sanitize_role("admin\nuser") is None
        assert _sanitize_role("admin\ruser") is None
        assert _sanitize_role("admin\n\ruser") is None

    def test_tab_inside_role_rejected(self):
        """Outer whitespace is stripped, but a tab *inside* a role
        fails the allowlist."""
        assert _sanitize_role("ad\tmin") is None

    def test_emoji_rejected(self):
        assert _sanitize_role("admin😎") is None

    def test_fullwidth_underscore_collapses(self):
        """Fullwidth ``＿`` (U+FF3F) collapses to ASCII ``_`` under NFKC,
        so a role containing it is rescued."""
        assert _sanitize_role("portfolio＿manager") == "portfolio_manager"

    def test_fullwidth_hyphen_collapses(self):
        """Fullwidth ``－`` (U+FF0D) collapses to ASCII ``-`` under NFKC."""
        assert _sanitize_role("role－with－dashes") == "role-with-dashes"

    def test_normalised_form_is_lowercased(self):
        """NFKC of fullwidth uppercase letters produces ASCII uppercase;
        the sanitiser additionally lowercases."""
        assert _sanitize_role("ＡＤＭＩＮ") == "admin"


# ---------------------------------------------------------------------------
# 4. _sanitize_role — size cap (no truncation)
# ---------------------------------------------------------------------------


class TestSanitizeRoleSizeCap:
    """Strings longer than 64 chars must be **rejected** (return
    ``None``), not silently truncated. Truncation was the prior
    behaviour and it allowed a 10 kB payload to sneak through after
    being chopped to 64 chars."""

    def test_one_char_accepted(self):
        assert _sanitize_role("a") == "a"

    def test_two_chars_accepted(self):
        assert _sanitize_role("ab") == "ab"

    def test_exactly_64_chars_accepted(self):
        role = "a" * 64
        assert _sanitize_role(role) == role

    def test_65_chars_rejected(self):
        role = "a" * 65
        assert _sanitize_role(role) is None

    def test_100_chars_rejected(self):
        role = "a" * 100
        assert _sanitize_role(role) is None

    def test_1kb_payload_rejected(self):
        role = "a" * 1024
        assert _sanitize_role(role) is None

    def test_10kb_payload_rejected(self):
        """The DoS payload called out in the SEV-741 follow-up."""
        role = "a" * 10_000
        assert _sanitize_role(role) is None

    def test_1mb_payload_rejected(self):
        role = "a" * 1_000_000
        assert _sanitize_role(role) is None

    def test_oversize_payload_not_truncated(self):
        """Guard against silent reintroduction of truncation."""
        role = "a" * 100
        result = _sanitize_role(role)
        assert result is None, (
            "Oversized input must return None, not a truncated substring "
            "(reintroducing truncation would re-open the SEV-741 DoS vector)."
        )

    def test_exactly_64_chars_with_unicode_normalized(self):
        """A 64-char post-NFKC string with fullwidth chars is accepted
        (the length check runs *after* NFKC)."""
        # 32 fullwidth 'a' (each NFKC -> 1 ASCII 'a') + 32 ASCII 'a' = 64 chars
        role = "ａ" * 32 + "a" * 32
        assert _sanitize_role(role) == "a" * 64

    def test_normalised_form_oversize_rejected(self):
        """If post-NFKC normalisation produces >64 chars, reject."""
        # 65 fullwidth 'a' -> 65 ASCII 'a' after NFKC
        role = "ａ" * 65
        assert _sanitize_role(role) is None


# ---------------------------------------------------------------------------
# 5. _sanitize_role — injection-resistance
# ---------------------------------------------------------------------------


class TestSanitizeRoleInjectionResistance:
    """The allowlist is the *only* defence — we never want a regex
    escape, denylist omission or DB-specific quote to leak a payload
    through. Test the canonical injection primitives explicitly."""

    @pytest.mark.parametrize(
        "value",
        [
            "admin' OR '1'='1",
            "admin\"; DROP TABLE users; --",
            "admin\\",
            "admin\\'",
            "admin\\\"",
            "admin/*",
            "admin*/",
            "${jndi:ldap://evil.com}",
            "%24%7Bjndi%3A%7D",
            "../../../etc/passwd",
            "../../admin",
            "<script>alert(1)</script>",
            "<svg/onload=alert(1)>",
            "javascript:alert(1)",
            "data:text/html,<script>",
            "admin\u202E\u0064\u006D\u0069\u006E",  # RLO + admin
        ],
    )
    def test_injection_payloads_rejected(self, value):
        assert _sanitize_role(value) is None

    def test_path_separator_rejected(self):
        assert _sanitize_role("../admin") is None
        assert _sanitize_role("..\\admin") is None

    def test_template_expression_rejected(self):
        assert _sanitize_role("{{admin}}") is None
        assert _sanitize_role("${admin}") is None
        assert _sanitize_role("#{admin}") is None


# ---------------------------------------------------------------------------
# 6. _ROLE_PATTERN — regex contract
# ---------------------------------------------------------------------------


class TestRolePatternContract:
    """Pin the allowlist regex itself — operators must be alerted if
    someone widens it (e.g. back to the denylist approach)."""

    def test_pattern_compiled(self):
        import re

        assert isinstance(_ROLE_PATTERN, re.Pattern)

    def test_pattern_source_is_strict_allowlist(self):
        """The regex source must be the exact SEV-741 string."""
        assert _ROLE_PATTERN.pattern == r"^[A-Za-z0-9_-]{1,64}$"

    def test_pattern_no_unicode_flag(self):
        """The pattern must not be compiled with ``re.UNICODE`` /
        ``re.W`` — that would let ``\\w`` match non-ASCII letters."""
        # ``re.UNICODE`` is the default in Python 3; what we are
        # really guarding against is the *pattern* containing ``\\w``
        # which under UNICODE matches Cyrillic / Greek. The literal
        # ``[A-Za-z0-9_-]`` is ASCII-only by definition.
        assert "\\w" not in _ROLE_PATTERN.pattern
        assert "\\W" not in _ROLE_PATTERN.pattern

    @pytest.mark.parametrize(
        ("value", "matches"),
        [
            ("a", True),
            ("admin", True),
            ("portfolio_manager", True),
            ("role-with-dashes", True),
            ("Role123", True),
            ("123456", True),
            ("", False),
            ("a" * 64, True),
            ("a" * 65, False),
            ("a b", False),
            ("a.b", False),
            ("a/b", False),
            ("a\\b", False),
            ("a'b", False),
            ('a"b', False),
            ("a:b", False),
            ("a;b", False),
        ],
    )
    def test_pattern_selectivity(self, value, matches):
        assert bool(_ROLE_PATTERN.match(value)) is matches


# ---------------------------------------------------------------------------
# 7. _should_overwrite_role — None current_role requires opt-in
# ---------------------------------------------------------------------------


class TestShouldOverwriteRoleNoneHandling:
    """SEV-741 follow-up: ``current_role is None`` on an *existing*
    user must not be a short-circuit. The helper is only invoked on
    the existing-user branch of every federated provider, so a
    ``None`` role at this point is a legacy row that pre-dates the
    NOT NULL constraint — populating it would be a privilege-
    escalation vector."""

    def test_none_role_blocked_when_opted_out(self):
        assert _should_overwrite_role(None, "user", _SettingsStub(overwrite=False)) is False

    def test_none_role_allowed_when_opted_in(self):
        assert _should_overwrite_role(None, "user", _SettingsStub(overwrite=True)) is True

    def test_none_role_with_admin_blocked_when_opted_out(self):
        """Critical: even when the IdP asserts the highest-privilege
        role, a legacy empty role must not be silently populated
        without operator opt-in."""
        assert _should_overwrite_role(None, "admin", _SettingsStub(overwrite=False)) is False

    def test_none_role_with_admin_allowed_when_opted_in(self):
        assert _should_overwrite_role(None, "admin", _SettingsStub(overwrite=True)) is True

    def test_same_role_short_circuits_even_when_opted_in(self):
        """The same-role short-circuit beats the opt-in flag — a
        no-op write is never emitted."""
        assert _should_overwrite_role("user", "user", _SettingsStub(overwrite=True)) is False

    def test_none_to_none_is_treated_as_same_role(self):
        """``None == None`` is True, so the helper returns False —
        no-op even with opt-in."""
        assert _should_overwrite_role(None, None, _SettingsStub(overwrite=True)) is False

    def test_missing_attribute_defaults_to_false(self):
        class _BareConfig:
            pass

        assert _should_overwrite_role("user", "admin", _BareConfig()) is False

    def test_missing_attribute_with_none_role_defaults_to_false(self):
        class _BareConfig:
            pass

        assert _should_overwrite_role(None, "admin", _BareConfig()) is False

    def test_truthy_non_bool_setting_treated_as_opted_in(self):
        class _TruthyConfig:
            auth_overwrite_role_on_login = 1  # truthy

        assert _should_overwrite_role("user", "admin", _TruthyConfig()) is True

    def test_falsy_non_bool_setting_treated_as_opted_out(self):
        class _FalsyConfig:
            auth_overwrite_role_on_login = 0

        assert _should_overwrite_role("user", "admin", _FalsyConfig()) is False

    def test_demotion_blocked_when_opted_out(self):
        """Even when the IdP asserts a *lower* privilege role, the
        helper refuses without opt-in (defence against downgrade
        attacks via compromised IdP)."""
        assert _should_overwrite_role("admin", "user", _SettingsStub(overwrite=False)) is False

    def test_demotion_allowed_when_opted_in(self):
        assert _should_overwrite_role("admin", "user", _SettingsStub(overwrite=True)) is True

    def test_escalation_blocked_when_opted_out(self):
        assert _should_overwrite_role("user", "admin", _SettingsStub(overwrite=False)) is False

    def test_escalation_allowed_when_opted_in(self):
        assert _should_overwrite_role("user", "admin", _SettingsStub(overwrite=True)) is True


# ---------------------------------------------------------------------------
# 8. map_roles end-to-end with the new sanitiser
# ---------------------------------------------------------------------------


class TestMapRolesEndToEnd:
    """End-to-end: every input flows through ``_sanitize_role`` and
    the ALLOWED_ROLES closed-set check, then collapses to ``user``."""

    def _provider(self):
        from engine.api.auth.base import IAuthProvider

        class _P(IAuthProvider):
            @property
            def name(self) -> str:
                return "test"

            async def authenticate(self, **_kwargs):  # pragma: no cover
                from engine.api.auth.base import AuthResult

                return AuthResult()

        return _P()

    def test_fullwidth_admin_claims_admin(self):
        """NFKC + lowercasing collapses fullwidth 'ａｄｍｉｎ' to 'admin'
        which is in ALLOWED_ROLES, so map_roles returns 'admin'."""
        assert self._provider().map_roles(["ａｄｍｉｎ"]) == "admin"

    def test_bidi_override_role_collapses_to_user(self):
        """Spoofed role with U+202E fails sanitisation, falls back
        to ``user`` — no escalation."""
        assert self._provider().map_roles([chr(0x202E) + "admin"]) == "user"

    def test_oversized_role_collapses_to_user(self):
        """A 65-char role fails sanitisation and falls back to ``user``."""
        assert self._provider().map_roles(["a" * 65]) == "user"

    def test_oversized_role_with_valid_alternative(self):
        """When one claim is oversized and another is valid, the
        valid one wins (oversized is dropped silently)."""
        assert (
            self._provider().map_roles(["a" * 65, "admin"]) == "admin"
        )

    def test_injected_payload_collapses_to_user(self):
        assert (
            self._provider().map_roles(["admin' OR '1'='1"]) == "user"
        )

    def test_non_string_role_in_list_collapses_to_user(self):
        """Non-string entries are rejected by the sanitiser; the rest
        of the list is still considered."""
        assert self._provider().map_roles([42, "admin"]) == "admin"

    def test_mixed_spoofed_and_valid_returns_highest_valid(self):
        assert (
            self._provider().map_roles(
                [
                    chr(0x202E) + "admin",  # spoofed
                    "viewer",  # valid
                    "developer",  # valid, higher priority
                    "a" * 100,  # oversize
                ]
            )
            == "developer"
        )

    def test_homoglyph_admin_collapses_to_user(self):
        """Cyrillic-а spoof of 'admin' fails sanitisation, falls back."""
        spoof = "\u0430" + "dmin"
        assert self._provider().map_roles([spoof]) == "user"


# ---------------------------------------------------------------------------
# 9. OIDC raw_roles claim shape normalisation
# ---------------------------------------------------------------------------


def _build_oidc_mock_client(rsa_keys, id_token_claims):
    """Reuse the OIDC test infrastructure from test_oidc_auth.py.

    ``rsa_keys`` is either a ``(private, public)`` tuple as produced by
    the canonical helper in ``tests/test_oidc_auth.py``, or just a
    private key from which we derive the public key.
    """
    import jwt
    from jwt.algorithms import RSAAlgorithm

    if isinstance(rsa_keys, tuple):
        private_key, pub_key = rsa_keys
    else:
        private_key = rsa_keys
        pub_key = private_key.public_key()
    kid = "test-kid-shape"
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    claims = {"aud": "test-client-id", **id_token_claims}
    id_token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    discovery = {
        "authorization_endpoint": "https://id.example.com/authorize",
        "token_endpoint": "https://id.example.com/token",
        "jwks_uri": "https://id.example.com/jwks",
    }

    class _Resp:
        def __init__(self, data=None, exc=None):
            self._data = data
            self._exc = exc

        def raise_for_status(self):
            if self._exc:
                raise self._exc

        def json(self):
            return self._data

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            pass

        async def get(self, _url, **_kw):
            if not hasattr(self, "_seen_get"):
                self._seen_get = True
                return _Resp(data=discovery)
            return _Resp(data={"keys": [jwk_dict]})

        async def post(self, _url, **_kw):
            return _Resp(data={"id_token": id_token, "access_token": "at"})

    return _Client()


class TestOidcRawRolesShapeNormalization:
    """``engine/api/auth/oidc.py`` normalises the ``raw_roles`` claim
    before handing it to ``map_roles``. Pin every shape it must
    accept / reject."""

    @pytest.fixture
    def provider(self):
        from engine.api.auth.oidc import OIDCAuthProvider

        return OIDCAuthProvider()

    @pytest.fixture
    def mock_settings(self, monkeypatch):
        s = Settings(
            oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
            oidc_client_id="test-client-id",
            oidc_client_secret="test-client-secret",
            oidc_redirect_uri="https://app.example.com/callback",
            oidc_role_claim="roles",
        )
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)
        return s

    @pytest.fixture
    def rsa_keys(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return private_key, private_key.public_key()

    async def _run_auth(self, provider, mock_settings, rsa_keys, claims):
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users: list[Any] = []
        mock_db.add = MagicMock(side_effect=created_users.append)

        client = _build_oidc_mock_client(rsa_keys, claims)
        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.authenticate(code="auth-code", db=mock_db)

        return result, created_users

    async def test_string_role_claim_wrapped_to_list(
        self, provider, mock_settings, rsa_keys
    ):
        """A single-string ``roles`` claim (Auth0 convention) is
        wrapped to a one-element list before being mapped."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s1", "email": "s@x.com", "roles": "admin"},
        )
        assert result.success is True
        assert created[0].role == "admin"

    async def test_list_role_claim_passed_through(
        self, provider, mock_settings, rsa_keys
    ):
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s2", "email": "l@x.com", "roles": ["viewer", "developer"]},
        )
        assert result.success is True
        # highest priority wins
        assert created[0].role == "developer"

    async def test_dict_role_claim_falls_back_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        """A dict claim (Keycloak-style ``realm_access.roles``) at
        the top level is not a list nor a string and falls back to
        ``user``."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s3", "email": "d@x.com", "roles": {"realm": "admin"}},
        )
        assert result.success is True
        assert created[0].role == "user"

    async def test_int_role_claim_falls_back_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s4", "email": "i@x.com", "roles": 42},
        )
        assert result.success is True
        assert created[0].role == "user"

    async def test_none_role_claim_falls_back_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        """A claim that is explicitly ``None`` falls back to user."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s5", "email": "n@x.com", "roles": None},
        )
        assert result.success is True
        assert created[0].role == "user"

    async def test_missing_role_claim_falls_back_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        """If the configured claim name is absent from the token,
        ``.get()`` returns ``[]`` (the default) and map_roles
        collapses to ``user``."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s6", "email": "m@x.com"},
        )
        assert result.success is True
        assert created[0].role == "user"

    async def test_empty_list_role_claim_falls_back_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s7", "email": "e@x.com", "roles": []},
        )
        assert result.success is True
        assert created[0].role == "user"

    async def test_empty_string_role_claim_falls_back_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        """An empty-string claim is wrapped to ``[""]`` and sanitiser
        rejects the empty string → fallback to ``user``."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s8", "email": "es@x.com", "roles": ""},
        )
        assert result.success is True
        assert created[0].role == "user"

    async def test_list_with_non_string_entries(
        self, provider, mock_settings, rsa_keys
    ):
        """A list containing non-strings (dict, int, None) — only the
        string entries are mapped, the rest are silently dropped."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s9", "email": "mx@x.com", "roles": ["admin", 42, None, {"x": 1}]},
        )
        assert result.success is True
        assert created[0].role == "admin"

    async def test_fullwidth_role_claim_normalized(
        self, provider, mock_settings, rsa_keys
    ):
        """Fullwidth 'ａｄｍｉｎ' in a claim is NFKC-normalised to 'admin'
        by the sanitiser and persisted as 'admin'."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s10", "email": "fw@x.com", "roles": ["ａｄｍｉｎ"]},
        )
        assert result.success is True
        assert created[0].role == "admin"

    async def test_spoofed_role_claim_collapses_to_user(
        self, provider, mock_settings, rsa_keys
    ):
        """An RLO-spoofed claim collapses to ``user`` — no escalation."""
        result, created = await self._run_auth(
            provider,
            mock_settings,
            rsa_keys,
            {"sub": "s11", "email": "sp@x.com", "roles": [chr(0x202E) + "admin"]},
        )
        assert result.success is True
        assert created[0].role == "user"


# ---------------------------------------------------------------------------
# 10. is_active ordering — disabled user never mutates role
# ---------------------------------------------------------------------------


class TestIsActiveCheckedBeforeRoleMutation:
    """SEV-741 follow-up: in every federated provider, the
    ``is_active`` flag must be evaluated **before**
    ``_should_overwrite_role`` is called, so a disabled account never
    produces a role-overwrite audit event and never flushes the
    role mutation to the DB.

    We test the ordering by setting ``auth_overwrite_role_on_login=True``
    and asserting that with a disabled user the role is *not* mutated
    and ``db.flush`` is *not* called.
    """

    @pytest.fixture
    def opted_in_settings(self, monkeypatch):
        return Settings(auth_overwrite_role_on_login=True)

    def _full_oidc_settings(self, monkeypatch):
        """OIDC requires discovery/JWKS URLs — wire a complete Settings."""
        s = Settings(
            oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
            oidc_client_id="test-client-id",
            oidc_client_secret="test-client-secret",
            oidc_redirect_uri="https://app.example.com/callback",
            oidc_role_claim="roles",
            auth_overwrite_role_on_login=True,
        )
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)
        return s

    # -- OIDC ----------------------------------------------------------------

    async def test_oidc_disabled_user_role_not_mutated(
        self, opted_in_settings, monkeypatch
    ):
        from engine.db.models import User

        self._full_oidc_settings(monkeypatch)

        # We bypass the http/JWKS machinery by patching authenticate's
        # external calls. The simplest path: directly test the
        # existing-user branch by stubbing the OIDC provider's
        # internals — but the easiest portable test is via LDAP which
        # has no async HTTP plumbing.
        # Instead: use a minimal end-to-end mock.
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from jwt.algorithms import RSAAlgorithm

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        kid = "disabled-kid"
        jwk_dict = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
        jwk_dict["kid"] = kid
        id_token = jwt.encode(
            {"aud": "test-client-id", "sub": "disabled-oidc", "email": "d@x.com",
             "roles": ["admin"]},
            private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

        discovery = {
            "authorization_endpoint": "https://id.example.com/authorize",
            "token_endpoint": "https://id.example.com/token",
            "jwks_uri": "https://id.example.com/jwks",
        }

        class _Resp:
            def __init__(self, data):
                self._d = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._d

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def get(self, _url, **_kw):
                if not hasattr(self, "_disc"):
                    self._disc = True
                    return _Resp(discovery)
                return _Resp({"keys": [jwk_dict]})

            async def post(self, _url, **_kw):
                return _Resp({"id_token": id_token, "access_token": "at"})

        # Pre-existing user that is DISABLED and currently a 'user'.
        disabled_user = User(
            email="d@x.com",
            display_name="Disabled",
            is_active=False,
            role="user",
            auth_provider="oidc",
            external_id="disabled-oidc",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        from engine.api.auth.oidc import OIDCAuthProvider

        with patch("httpx.AsyncClient", return_value=_Client()):
            result = await OIDCAuthProvider().authenticate(
                code="x", db=mock_db
            )

        assert result.success is False
        assert "disabled" in (result.error or "").lower()
        # Critical: role must NOT have been mutated, and no flush
        # should have been called.
        assert disabled_user.role == "user"
        mock_db.flush.assert_not_called()

    # -- LDAP ----------------------------------------------------------------

    def _build_ldap_mocks(self, attrs):
        mock_ldap = MagicMock()
        mock_ldap.initialize = MagicMock(
            return_value=_FakeLDAPConn(search_results=[("uid=x", attrs)])
        )
        mock_ldap.OPT_NETWORK_TIMEOUT = 7
        mock_ldap.OPT_TIMEOUT = 8
        mock_ldap.SCOPE_SUBTREE = 2
        mock_filter = MagicMock()
        mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
        return mock_ldap, mock_filter

    async def test_ldap_disabled_user_role_not_mutated(
        self, opted_in_settings, monkeypatch
    ):
        from engine.db.models import User

        s = Settings(
            ldap_server_url="ldap://x",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping=json.dumps(
                {"cn=admins,ou=groups,dc=example,dc=com": "admin"}
            ),
            auth_overwrite_role_on_login=True,
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = {
            "uid": [b"x"],
            "mail": [b"d@x.com"],
            "cn": [b"D"],
            "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
        }
        mock_ldap, mock_filter = self._build_ldap_mocks(attrs)

        disabled_user = User(
            email="d@x.com",
            display_name="D",
            is_active=False,
            role="user",
            auth_provider="ldap",
            external_id="x",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        from engine.api.auth.ldap import LDAPAuthProvider

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="x", password="p", db=mock_db
            )

        assert result.success is False
        assert "disabled" in (result.error or "").lower()
        assert disabled_user.role == "user"
        mock_db.flush.assert_not_called()

    # -- Google --------------------------------------------------------------

    async def test_google_disabled_user_role_not_mutated(
        self, opted_in_settings, monkeypatch
    ):
        from engine.db.models import User

        monkeypatch.setattr("engine.api.auth.google.settings", opted_in_settings)

        # Patch httpx so the Google flow returns a deterministic
        # userinfo payload.
        class _Resp:
            def __init__(self, data):
                self._d = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._d

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def post(self, _url, **_kw):
                return _Resp({"access_token": "at"})

            async def get(self, _url, **_kw):
                return _Resp({"sub": "g-disabled", "email": "d@x.com", "name": "D"})

        disabled_user = User(
            email="d@x.com",
            display_name="D",
            is_active=False,
            role="user",
            auth_provider="google",
            external_id="g-disabled",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        from engine.api.auth.google import GoogleAuthProvider

        with patch("httpx.AsyncClient", return_value=_Client()):
            result = await GoogleAuthProvider().authenticate(
                code="x", db=mock_db
            )

        assert result.success is False
        assert "disabled" in (result.error or "").lower()
        assert disabled_user.role == "user"
        mock_db.flush.assert_not_called()

    # -- GitHub --------------------------------------------------------------

    async def test_github_disabled_user_role_not_mutated(
        self, opted_in_settings, monkeypatch
    ):
        from engine.db.models import User

        monkeypatch.setattr("engine.api.auth.github_oauth.settings", opted_in_settings)

        class _Resp:
            def __init__(self, data):
                self._d = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._d

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def post(self, _url, **_kw):
                return _Resp({"access_token": "at"})

            async def get(self, _url, **_kw):
                return _Resp({"id": 12345, "email": "d@x.com", "name": "D"})

        disabled_user = User(
            email="d@x.com",
            display_name="D",
            is_active=False,
            role="user",
            auth_provider="github",
            external_id="12345",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        from engine.api.auth.github_oauth import GitHubAuthProvider

        with patch("httpx.AsyncClient", return_value=_Client()):
            result = await GitHubAuthProvider().authenticate(
                code="x", db=mock_db
            )

        assert result.success is False
        assert "disabled" in (result.error or "").lower()
        assert disabled_user.role == "user"
        mock_db.flush.assert_not_called()

    # -- Static ordering guards ---------------------------------------------

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    def test_is_active_check_precedes_overwrite_in_source(
        self, module_path, class_name
    ):
        """Static-analysis guard: in the provider's source text, the
        ``is_active`` check must appear *textually before* the
        ``_should_overwrite_role`` call. This catches accidental
        re-ordering during refactoring."""
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        is_active_idx = src.find("is_active")
        overwrite_idx = src.find("_should_overwrite_role(")
        assert is_active_idx != -1, f"{module_path} lost its is_active check"
        assert overwrite_idx != -1, f"{module_path} lost its _should_overwrite_role call"
        assert is_active_idx < overwrite_idx, (
            f"{module_path}: is_active check must appear *before* "
            "_should_overwrite_role in the source so a disabled user "
            "never triggers a role-overwrite audit event."
        )


# ---------------------------------------------------------------------------
# 11. _FakeLDAPConn helper (mirrors tests/test_ldap_auth.py)
# ---------------------------------------------------------------------------


class _FakeLDAPConn:
    """Minimal LDAP connection fake used by the LDAP-disabled-user
    ordering test above."""

    def __init__(self, search_results=None):
        self._results = search_results or []
        self._opts: dict[int, Any] = {}

    def set_option(self, opt: int, value: Any) -> None:
        self._opts[opt] = value

    def simple_bind_s(self, _dn: str, _password: str) -> None:
        return None

    def search_s(self, _base: str, _scope: int, _filter: str, _attrs: list[str]):
        return self._results

    def unbind_s(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 12. Cross-cutting: sanitiser is wired into every provider's map_roles
# ---------------------------------------------------------------------------


class TestSanitiserWiredIntoEveryProvider:
    """Static guard: every concrete provider must inherit ``map_roles``
    from ``IAuthProvider`` (which uses ``_sanitize_role``). A
    provider that overrides ``map_roles`` and bypasses the sanitiser
    would defeat the whole defence-in-depth chain."""

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
            ("engine.api.auth.local", "LocalAuthProvider"),
        ],
    )
    def test_provider_does_not_override_map_roles(self, module_path, class_name):
        import importlib

        from engine.api.auth.base import IAuthProvider

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        # ``map_roles`` is defined on IAuthProvider. If a subclass
        # overrides it, ``map_roles`` will be in ``cls.__dict__``.
        assert "map_roles" not in cls.__dict__, (
            f"{class_name} must not override map_roles — doing so "
            "bypasses _sanitize_role and re-opens SEV-741."
        )
        assert issubclass(cls, IAuthProvider)

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    def test_provider_map_roles_rejects_spoofed_input(self, module_path, class_name):
        """End-to-end: each provider's inherited ``map_roles`` collapses
        spoofed input to ``user``."""
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        provider = cls()
        assert provider.map_roles([chr(0x202E) + "admin"]) == "user"
        assert provider.map_roles(["a" * 65]) == "user"
        assert provider.map_roles(["ａｄｍｉｎ"]) == "admin"


# ---------------------------------------------------------------------------
# 13. Hypothesis-style property tests (no dependency on hypothesis library)
# ---------------------------------------------------------------------------


class TestSanitiserProperties:
    """Cheap property-style tests — pin invariants without hypothesis."""

    def test_idempotent_on_ascii_input(self):
        """``_sanitize_role(_sanitize_role(x)) == _sanitize_role(x)``
        for every allowed-role string."""
        for role in ALLOWED_ROLES:
            once = _sanitize_role(role)
            twice = _sanitize_role(once)  # type: ignore[arg-type]
            assert once == twice

    def test_only_returns_strings_or_none(self):
        """The return type is exactly ``str | None``."""
        for value in ["admin", "", None, 42, "a" * 100, "ａｄｍｉｎ", "a;b"]:
            result = _sanitize_role(value)  # type: ignore[arg-type]
            assert result is None or isinstance(result, str)

    def test_returned_string_is_always_lowercase(self):
        """If the sanitiser returns a string, it is always lowercase."""
        for value in ["ADMIN", "Admin", "aDmIn", "ＡＤＭＩＮ"]:
            result = _sanitize_role(value)
            if result is not None:
                assert result == result.lower()

    def test_returned_string_is_always_within_size_limit(self):
        for value in ["a" * 64, "a" * 65, "a" * 100, "ａ" * 64, "ａ" * 65]:
            result = _sanitize_role(value)
            if result is not None:
                assert len(result) <= 64
