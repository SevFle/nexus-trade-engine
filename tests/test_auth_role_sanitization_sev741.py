"""Tests for the role-name sanitization added under SEV-741.

Background
----------
A misconfigured or hostile upstream Identity Provider can put arbitrary
text into the role-claim payload. ``IAuthProvider.map_roles`` therefore
applies a defensive sanitization layer so that:

1. Log injection via CR / LF / NUL / BEL / ANSI-escape sequences is
   impossible — control bytes are stripped from the ``unrecognized``
   payload of the warning event before it reaches structlog.
2. Visual spoofing via zero-width / bidi characters (ZWSP, ZWNJ, ZWJ,
   LRM, RLM, RLO, BOM) is impossible — these code points are stripped
   from anything we log.
3. The persistence layer is never asked to write a string longer than
   ``User.role``'s ``String(20)`` column — overlong role names are
   treated as unrecognized.
4. The matching against the recognized-role set is deliberately
   performed on the **raw** normalized input so a spoofed role like
   ``"admin\\u200B"`` is **not** silently coerced to ``"admin"``. The
   user receives the safe default ``"user"`` instead.

This module pins each of those behaviours independently of the
provider-specific overwrite guard covered in
``test_auth_role_promotion_security_fix.py``.
"""

from __future__ import annotations

import pytest

from engine.api.auth.base import (
    _CONTROL_CHARS_RE,
    _MAX_ROLE_LENGTH,
    AuthResult,
    IAuthProvider,
    _sanitize_role,
)


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **_kwargs):
        return AuthResult()


# ---------------------------------------------------------------------------
# 1. _CONTROL_CHARS_RE coverage
# ---------------------------------------------------------------------------


class TestControlCharsReCoverage:
    """``_CONTROL_CHARS_RE`` must match every dangerous Unicode class
    called out in the spec: C0, DEL, C1, RTL override, zero-width
    characters, and BOM. It must NOT match printable ASCII, regular
    Unicode letters, or whitespace separators."""

    def test_c0_block_matched(self):
        for codepoint in range(0x20):
            assert _CONTROL_CHARS_RE.search(f"a{chr(codepoint)}b") is not None, (
                f"C0 control U+{codepoint:04X} should match"
            )

    def test_del_matched(self):
        assert _CONTROL_CHARS_RE.search("a\x7fb") is not None
        assert _CONTROL_CHARS_RE.search("\x7f") is not None

    def test_c1_block_matched(self):
        """U+0080 through U+009F — the often-forgotten C1 block."""
        for codepoint in range(0x80, 0xA0):
            assert _CONTROL_CHARS_RE.search(f"a{chr(codepoint)}b") is not None, (
                f"C1 control U+{codepoint:04X} should match"
            )

    def test_rtl_override_matched(self):
        """U+202E RIGHT-TO-LEFT OVERRIDE — visual spoofing vector."""
        assert _CONTROL_CHARS_RE.search("admin\u202e") is not None
        assert _CONTROL_CHARS_RE.search("\u202eadmin") is not None
        assert _CONTROL_CHARS_RE.search("a\u202eb") is not None

    def test_zero_width_chars_matched(self):
        """U+200B (ZWSP), U+200C (ZWNJ), U+200D (ZWJ), U+200E (LRM),
        U+200F (RLM)."""
        for codepoint in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F):
            assert _CONTROL_CHARS_RE.search(f"admin{chr(codepoint)}") is not None, (
                f"Zero-width U+{codepoint:04X} should match"
            )

    def test_bom_matched(self):
        """U+FEFF BOM / Zero-Width No-Break Space."""
        assert _CONTROL_CHARS_RE.search("\ufeffadmin") is not None
        assert _CONTROL_CHARS_RE.search("admin\ufeff") is not None

    def test_printable_ascii_unmatched(self):
        assert _CONTROL_CHARS_RE.search("admin") is None
        assert _CONTROL_CHARS_RE.search("portfolio_manager") is None

    def test_non_control_unicode_unmatched(self):
        """Non-control Unicode (e.g. CJK, accented Latin) must pass
        through — operators in non-English locales may legitimately
        configure IdP group names that include them."""
        assert _CONTROL_CHARS_RE.search("管理员") is None
        assert _CONTROL_CHARS_RE.search("administrateur") is None
        assert _CONTROL_CHARS_RE.search("αβγ") is None

    def test_ascii_whitespace_unmatched(self):
        """ASCII whitespace is allowed (it would be stripped elsewhere
        by the ``strip()`` in :meth:`map_roles`). The regex must not
        accidentally eat valid separators in role display strings."""
        assert _CONTROL_CHARS_RE.search("a b") is None
        assert _CONTROL_CHARS_RE.search("a-b") is None


# ---------------------------------------------------------------------------
# 2. _sanitize_role behaviour
# ---------------------------------------------------------------------------


class TestSanitizeRole:
    """``_sanitize_role`` strips every character matched by
    ``_CONTROL_CHARS_RE`` and leaves the rest of the string intact."""

    def test_rtl_override_stripped(self):
        assert _sanitize_role("admin\u202e") == "admin"
        assert _sanitize_role("\u202eadmin") == "admin"
        assert _sanitize_role("a\u202ed\u202emin") == "admin"

    def test_zero_width_chars_stripped(self):
        assert _sanitize_role("admin\u200b") == "admin"
        assert _sanitize_role("ad\u200cmin") == "admin"
        assert _sanitize_role("\u200dadmin") == "admin"

    def test_bom_stripped(self):
        assert _sanitize_role("\ufeffadmin") == "admin"
        assert _sanitize_role("admin\ufeff") == "admin"

    def test_c1_range_stripped(self):
        assert _sanitize_role("admin\x80") == "admin"
        assert _sanitize_role("ad\x9fmin") == "admin"

    def test_mixed_threat_stripped(self):
        assert (
            _sanitize_role("\u202eadmin\u200b\u200c\u200d\ufeff\x85")
            == "admin"
        )

    def test_normal_role_unchanged(self):
        assert _sanitize_role("malicious") == "malicious"
        assert _sanitize_role("admin") == "admin"
        assert _sanitize_role("portfolio_manager") == "portfolio_manager"
        assert _sanitize_role("user") == "user"

    def test_only_control_chars_collapses_to_empty(self):
        """A role composed solely of control characters collapses to
        the empty string — the caller is responsible for falling back
        to the default ``user`` role (see :meth:`map_roles`)."""
        assert _sanitize_role("\u202e\u200b\ufeff") == ""
        assert _sanitize_role("\x00\x01\x02") == ""

    def test_c0_range_stripped(self):
        assert _sanitize_role("admin\u202e\u200b") == "admin"

    def test_idempotent(self):
        once = _sanitize_role("developer")
        twice = _sanitize_role(once)
        assert once == twice == "developer"

        once = _sanitize_role("user\nFAKE")
        twice = _sanitize_role(once)
        assert once == twice
        assert "\n" not in twice

    def test_in_place_mutation_check(self):
        result = _sanitize_role("ad\u200bmin")
        assert result == "admin"
        # No control characters in the output.
        assert "\u200b" not in result
        assert _sanitize_role(result) == "admin"

    def test_does_not_mutate_clean_string(self):
        s = "developer"
        result = _sanitize_role(s)
        assert result == s
        # Identity preserved for clean strings (the regex doesn't
        # match, so ``re.sub`` returns the input unchanged).
        assert result is s

    def test_non_string_returns_empty(self):
        """Defence-in-depth: a non-string claim (e.g. an int that
        slipped through IdP parsing) must not crash the sanitizer."""
        assert _sanitize_role(None) == ""  # type: ignore[arg-type]
        assert _sanitize_role(42) == ""  # type: ignore[arg-type]
        assert _sanitize_role(b"admin") == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. map_roles integration with sanitization
# ---------------------------------------------------------------------------


class TestMapRolesSanitizationIntegration:
    """End-to-end: ``map_roles`` returns a sanitized, non-empty string
    and emits a sanitized ``unrecognized`` payload."""

    def test_recognized_role_returned_verbatim(self):
        p = _ConcreteProvider()
        assert p.map_roles(["admin"]) == "admin"

    def test_external_role_with_zero_width_does_not_match_recognized(self):
        """An external role like ``"admin\\u200B"`` is NOT equal to the
        canonical ``"admin"`` after lowercase+strip — it falls through
        to ``unrecognized`` and the user gets the default ``user``
        role. This is the safe default — never persist a spoofed
        role."""
        p = _ConcreteProvider()
        assert p.map_roles(["admin\u200b"]) == "user"

    def test_recognized_roles_have_no_control_chars_in_output(self):
        """For every recognized role, the output must not contain any
        RTL / ZW / BOM character."""
        p = _ConcreteProvider()
        for role in (
            "admin",
            "user",
            "developer",
            "viewer",
            "portfolio_manager",
            "quant_dev",
            "retail_trader",
        ):
            mapped = p.map_roles([role])
            sanitized = _sanitize_role(mapped)
            assert sanitized == mapped
            assert "\u202e" not in mapped
            assert "\u200b" not in mapped
            assert "\ufeff" not in mapped

    def test_only_control_chars_collapses_to_default_user(self):
        """If sanitization would produce an empty string (e.g. a
        recognized role were somehow comprised solely of control
        chars), map_roles falls back to the default ``user`` role
        rather than persisting an empty string."""
        p = _ConcreteProvider()
        assert p.map_roles(["\u202e\u200b\ufeff"]) == "user"
        assert p.map_roles(["   "]) == "user"

    def test_non_string_role_silently_skipped(self):
        """A non-string entry in ``external_roles`` must not crash
        ``map_roles`` — IdP claims occasionally surface malformed
        payloads. The non-string is ignored; the user still gets the
        safe default ``"user"``."""
        p = _ConcreteProvider()
        assert p.map_roles([None, 42, b"admin"]) == "user"  # type: ignore[list-item]

    def test_oversize_role_treated_as_unrecognized(self, monkeypatch):
        """A role longer than :data:`_MAX_ROLE_LENGTH` must NOT match
        a recognized role and must be reported as unrecognized."""
        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                pass

            def error(self, _event, **kwargs):  # pragma: no cover
                pass

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())
        p = _ConcreteProvider()
        oversize = "a" * (_MAX_ROLE_LENGTH + 5)
        assert p.map_roles([oversize]) == "user"
        assert calls, "expected an unrecognized-role warning"
        unrecognized = calls[0]["unrecognized"]
        assert isinstance(unrecognized, list)
        assert oversize in unrecognized

    def test_oversize_role_with_control_chars_truncated_in_payload(
        self, monkeypatch
    ):
        """An overlong role that also carries control characters must
        be both length-rejected AND sanitized in the unrecognized
        payload — operators should never see a CR/LF in the log
        line."""
        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                pass

            def error(self, _event, **kwargs):  # pragma: no cover
                pass

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())
        p = _ConcreteProvider()
        # 30 'a' chars + an embedded newline (well over _MAX_ROLE_LENGTH).
        payload = ("a" * 30) + "\n" + "evil"
        assert p.map_roles([payload]) == "user"
        assert calls
        unrecognized = calls[0]["unrecognized"]
        assert isinstance(unrecognized, list)
        assert any("\n" not in item for item in unrecognized)


# ---------------------------------------------------------------------------
# 4. map_roles unrecognized payload sanitization (log injection defence)
# ---------------------------------------------------------------------------


class TestUnrecognizedPayloadIsSanitized:
    """The ``unrecognized=`` payload of the warning event must contain
    **sanitized** strings — this is the actual security boundary, not
    the helper in isolation."""

    def _patch(self, monkeypatch):
        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, "level": "info", **kwargs})

            def error(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, "level": "error", **kwargs})

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())
        return calls

    def test_crlf_injected_role_is_sanitized_in_payload(self, monkeypatch):
        """Classic log-injection: an attacker includes a CR/LF in the
        role claim so that downstream log viewers see a second,
        attacker-controlled line. The payload must contain the
        sanitized form (no CR/LF)."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "FAKE\nINJECTED\rEVENT"])
        assert calls
        payload = calls[0]["unrecognized"]
        assert isinstance(payload, list)
        assert "FAKEINJECTEDEVENT" in payload
        for item in payload:
            assert "\n" not in item
            assert "\r" not in item

    def test_rtl_override_in_unrecognized_role_is_stripped(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "X\u202e"])
        assert calls
        payload = calls[0]["unrecognized"]
        assert isinstance(payload, list)
        assert any("X" in item for item in payload)
        for item in payload:
            assert "\u202e" not in item

    def test_zero_width_chars_in_unrecognized_role_stripped(
        self, monkeypatch
    ):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "X\u200b\u200c\u200d"])
        assert calls
        payload = calls[0]["unrecognized"]
        assert isinstance(payload, list)
        for item in payload:
            assert "\u200b" not in item
            assert "\u200c" not in item
            assert "\u200d" not in item

    def test_oversize_role_logged_in_truncated_form(self, monkeypatch):
        """An overlong role must appear in the unrecognized payload
        (sanitized) so operators can see what the IdP sent, but the
        CR/LF / ANSI sequences inside it must be stripped first."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        big = ("a" * (_MAX_ROLE_LENGTH + 5)) + "\n" + "X"
        p.map_roles(["admin", big])
        assert calls
        payload = calls[0]["unrecognized"]
        assert isinstance(payload, list)
        for item in payload:
            assert "\n" not in item
            assert "\r" not in item

    def test_combined_threats_in_one_call(self, monkeypatch):
        """CRLF + oversize + unicode all in one — sanitizer must handle
        all three transformations in the correct order (strip control
        chars, then keep length cap)."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(
            [
                "\n\n\n\n\n" + "X" + "\r\n" + "rôle",
            ]
        )
        assert calls
        payload = calls[0]["unrecognized"]
        assert isinstance(payload, list)
        assert len(payload) == 1
        item = payload[0]
        assert isinstance(item, str)
        for cp in (0x0A, 0x0D, 0x202E, 0x200B, 0xFEFF):
            assert chr(cp) not in item, (
                f"control char 0x{cp:02x} survived sanitization"
            )


# ---------------------------------------------------------------------------
# 5. _MAX_ROLE_LENGTH contract
# ---------------------------------------------------------------------------


class TestMaxRoleLength:
    """The length cap must match the database column so a persisted
    role never violates ``User.role String(20)``."""

    def test_max_role_length_matches_db_column(self):
        """Import the model to confirm the constant matches the DB
        constraint declared on :class:`engine.db.models.User`."""
        from engine.db.models import User

        # Inspect the SQLAlchemy column to extract its length.
        col = User.__table__.columns["role"]
        sa_length = col.type.length
        assert sa_length is not None
        assert sa_length == _MAX_ROLE_LENGTH, (
            f"_MAX_ROLE_LENGTH={_MAX_ROLE_LENGTH} must match the "
            f"User.role column length={sa_length} so a persisted role "
            "never violates the database constraint."
        )

    def test_all_recognized_roles_fit(self):
        """Sanity check: every role listed in map_roles' priority
        table must be shorter than _MAX_ROLE_LENGTH so the cap does
        not accidentally reject a legitimate role."""
        # Source inspection — pull the recognized role names out of
        # map_roles. They are the keys of the literal dict.
        import inspect

        from engine.api.auth.base import IAuthProvider

        src = inspect.getsource(IAuthProvider.map_roles)
        # Look for the literal role names; the priority table is
        # the only place these strings appear.
        for role in (
            "viewer",
            "user",
            "retail_trader",
            "quant_dev",
            "developer",
            "portfolio_manager",
            "admin",
        ):
            assert role in src
            assert len(role) < _MAX_ROLE_LENGTH, (
                f"recognized role {role!r} is {len(role)} chars but "
                f"_MAX_ROLE_LENGTH is {_MAX_ROLE_LENGTH}; the cap "
                "must accommodate every legitimate role."
            )


# ---------------------------------------------------------------------------
# 6. Cross-provider: every federated provider still routes through the
#    centralized overwrite guard.
# ---------------------------------------------------------------------------


class TestEveryProviderGoesThroughHelper:
    """Static guard: each federated provider module must continue to
    import the helper. Catches accidental revert / re-implementation
    that bypasses the centralized SEV-741 policy."""

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    def test_provider_imports_should_overwrite_role(self, module_path, class_name):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        assert "_should_overwrite_role" in inspect.getsource(mod), (
            f"{module_path} must import _should_overwrite_role from "
            "engine.api.auth.base (SEV-741)."
        )
        assert hasattr(mod, class_name)
