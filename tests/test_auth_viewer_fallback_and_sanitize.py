"""Tests for SEV-741 follow-up hardening of ``IAuthProvider.map_roles``.

Covers three defense-in-depth measures that sit on top of the original
SEV-741 fix (which removed the silent ``_ROLE_PROMOTIONS`` table):

1. **Viewer floor**: when no recognized role is present (empty list,
   whitespace-only entries, or wholly unrecognized roles) the function
   falls back to ``viewer`` — the *lowest-privilege* recognized role —
   rather than ``user``. This prevents an implicit upgrade for users
   whose IdP supplies an empty or wholly-unknown roles claim.

2. **Log sanitization — control characters**: unrecognized role
   strings are stripped of ASCII control characters (0x00-0x1F, 0x7F)
   before being reflected into the warning payload. This prevents a
   hostile or misconfigured upstream IdP from injecting newlines,
   ANSI escape sequences, or NUL bytes into operator log streams
   (log injection / log forging).

3. **Log sanitization — length cap**: unrecognized role strings are
   capped at 128 characters before logging. This bounds the size of
   the warning payload and defends against log-flooding attacks in
   which an upstream provider ships multi-kilobyte role strings.

These tests pin the contract so that a future refactor cannot
silently regress any of the three properties.
"""

from __future__ import annotations

import pytest

from engine.api.auth.base import AuthResult, IAuthProvider


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-sanitize"

    async def authenticate(self, **kwargs):  # pragma: no cover
        return AuthResult()


# ---------------------------------------------------------------------------
# 1. Viewer floor — empty / wholly-unrecognized input returns "viewer"
# ---------------------------------------------------------------------------


class TestViewerFloorFallback:
    """When no recognized role is supplied, the function must return
    ``viewer`` (the lowest-privilege recognized role), not ``user``.

    This is a privilege-boundary guard: an upstream IdP that supplies
    an empty roles claim (or one that consists entirely of unknown
    strings) should not silently grant a higher-privilege role than
    ``viewer``.
    """

    def test_empty_roles_claim_returns_viewer(self):
        """The primary contract pinned by this suite: an empty claim
        must return ``viewer``."""
        assert _ConcreteProvider().map_roles([]) == "viewer"

    def test_whitespace_only_roles_return_viewer(self):
        """Whitespace-only strings normalize to the empty string,
        which is not a known role. Fallback must be ``viewer``."""
        assert _ConcreteProvider().map_roles(["   "]) == "viewer"
        assert _ConcreteProvider().map_roles(["", "\t", "\n"]) == "viewer"

    def test_purely_unrecognized_roles_return_viewer(self):
        """Every entry is unknown — fallback must be ``viewer``."""
        assert (
            _ConcreteProvider().map_roles(["superuser", "root", "god"])
            == "viewer"
        )

    def test_single_unrecognized_role_returns_viewer(self):
        assert _ConcreteProvider().map_roles(["totally_bogus"]) == "viewer"

    def test_recognized_viewer_role_is_returned_verbatim(self):
        """A *recognized* ``viewer`` claim is not the same as the
        fallback: it must be returned as-is. This distinguishes
        'explicitly granted viewer' from 'default viewer floor'."""
        assert _ConcreteProvider().map_roles(["viewer"]) == "viewer"

    def test_mixed_recognized_and_unrecognized_uses_recognized(self):
        """When at least one recognized role is present, the highest
        such role wins — the viewer floor only applies when no
        recognized role is found."""
        assert (
            _ConcreteProvider().map_roles(["admin", "bogus_group"]) == "admin"
        )
        # The viewer floor does not override a higher recognized role.
        assert (
            _ConcreteProvider().map_roles(["developer", "weird_role"])
            == "developer"
        )


# ---------------------------------------------------------------------------
# 2. Log sanitization — control characters are stripped
# ---------------------------------------------------------------------------


class TestLogSanitizationControlChars:
    """Unrecognized role strings must have ASCII control characters
    stripped before they appear in the warning payload.

    Without this, a hostile IdP could embed newlines (``\\n``), carriage
    returns (``\\r``), ANSI escape sequences (``\\x1b[``), or NUL bytes
    inside a role string in order to forge additional log lines,
    corrupt terminal output, or trigger parser confusion downstream.
    """

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

    @pytest.mark.parametrize(
        "raw_role",
        [
            "bad\nrole",
            "bad\rrole",
            "bad\trole",
            "bad\x00role",
            "bad\x1b[31mrole\x1b[0m",  # ANSI "red"
            "bad\x7frole",  # DEL
            "bad\n\r\trole",
        ],
    )
    def test_control_chars_stripped_from_unrecognized_payload(
        self, monkeypatch, raw_role
    ):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        # Sanity: the input is indeed unrecognized.
        assert p.map_roles([raw_role]) == "viewer"
        assert calls, "Expected an unrecognized-role warning"
        unrecognized = calls[0]["unrecognized"]
        assert len(unrecognized) == 1, (
            f"Expected exactly one unrecognized entry for {raw_role!r}, "
            f"got {unrecognized!r}"
        )
        logged = unrecognized[0]
        # No control characters (0x00-0x1F or 0x7F) should remain.
        assert all((ord(c) >= 0x20 and ord(c) != 0x7F) for c in logged), (
            f"Control characters were not stripped: raw={raw_role!r}, "
            f"logged={logged!r}"
        )

    def test_log_injection_via_newline_is_neutralized(self, monkeypatch):
        """A role string containing a newline cannot inject a fake
        log line — the newline must be stripped before logging."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        # Craft a payload that, if unsanitized, would inject a fake
        # INFO line into a log aggregator that splits on newlines.
        p.map_roles(
            ["weird_role\nINFO auth.login.success user_id=admin"]
        )
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert "\n" not in logged, (
            f"Newline must be stripped to prevent log injection; "
            f"got {logged!r}"
        )
        # The injected content must not survive as a recognizable
        # event line — it is just part of the (now-single-line)
        # sanitized string.
        assert "INFO auth.login.success" in logged


# ---------------------------------------------------------------------------
# 3. Log sanitization — length cap (128 chars)
# ---------------------------------------------------------------------------


class TestLogSanitizationLengthCap:
    """Unrecognized role strings must be capped at 128 characters
    before they appear in the warning payload.

    This bounds the size of operator log streams and defends against
    log-flooding attacks in which a hostile IdP ships multi-kilobyte
    or multi-megabyte role strings.
    """

    _MAX_LEN = 128

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

    def test_short_unrecognized_role_is_not_truncated(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["short_bogus_role"])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert logged == "short_bogus_role"
        assert len(logged) <= self._MAX_LEN

    def test_long_unrecognized_role_is_truncated_to_128(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        long_role = "A" * 5000
        p.map_roles([long_role])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert len(logged) == self._MAX_LEN, (
            f"Expected truncated length {self._MAX_LEN}, got {len(logged)}"
        )

    def test_exactly_128_chars_is_not_truncated(self, monkeypatch):
        """Boundary: a string of exactly 128 characters should pass
        through without being shortened."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        boundary_role = "B" * self._MAX_LEN
        p.map_roles([boundary_role])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert len(logged) == self._MAX_LEN

    def test_over_128_chars_is_truncated(self, monkeypatch):
        """One character over the boundary must be truncated."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        over_role = "C" * (self._MAX_LEN + 1)
        p.map_roles([over_role])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert len(logged) == self._MAX_LEN

    def test_truncation_and_control_char_strip_compose(self, monkeypatch):
        """Sanitization is a single pass: control chars are stripped
        first, then the result is truncated. A payload that contains
        both control characters *and* exceeds the length cap must be
        both stripped and capped."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        # 200 'X' chars, with 50 newline characters sprinkled in.
        payload = ("X\n" * 50) + ("X" * 100)  # 50 newlines + 100 X
        p.map_roles([payload])
        assert calls
        logged = calls[0]["unrecognized"][0]
        # No control characters remain.
        assert "\n" not in logged
        # And the result is capped at 128.
        assert len(logged) <= self._MAX_LEN


# ---------------------------------------------------------------------------
# 4. Recognized roles are NOT subject to sanitization in the result
# ---------------------------------------------------------------------------


class TestRecognizedRolesNotMutated:
    """Sanitization only applies to *unrecognized* role strings (which
    are reflected in the warning payload). The function's return value
    is always drawn from the fixed ``role_priority`` table and is
    therefore safe by construction."""

    def test_recognized_admin_returned_as_is(self):
        assert _ConcreteProvider().map_roles(["admin"]) == "admin"

    def test_recognized_viewer_returned_as_is(self):
        assert _ConcreteProvider().map_roles(["viewer"]) == "viewer"

    def test_case_normalization_still_applies(self):
        """Pre-existing behavior: recognized roles are case-insensitive
        and whitespace-stripped. The new sanitization layer does not
        regress this."""
        assert _ConcreteProvider().map_roles(["  ADMIN  "]) == "admin"
        assert _ConcreteProvider().map_roles(["Admin"]) == "admin"
