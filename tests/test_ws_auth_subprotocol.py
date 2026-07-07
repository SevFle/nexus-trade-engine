"""Unit tests for WebSocket subprotocol-based handshake auth (SEV-275).

Covers :func:`engine.api.ws.auth._extract_token_from_handshake`, the
:func:`select_echo_subprotocol` helper and the
:class:`AmbiguousSubprotocolError` rejection contract.

The handshake model: a client offers credentials as a single
``bearer.<token>`` subprotocol (plus the constant ``auth.v1`` value the
server is allowed to echo back). The server must:

* accept **exactly one** bearer subprotocol,
* reject **ambiguous** multi-bearer handshakes rather than guessing, and
* never reflect the raw token back in its echoed subprotocol.
"""

from __future__ import annotations

import pytest

from engine.api.ws.auth import (
    _BEARER_SUBPROTOCOL_PREFIX,
    WS_AUTH_SUBPROTOCOL,
    AmbiguousSubprotocolError,
    _extract_token_from_handshake,
    select_echo_subprotocol,
)

# ---------------------------------------------------------------------------
# Happy path — single bearer subprotocol
# ---------------------------------------------------------------------------


class TestExtractTokenSingleBearer:
    def test_single_bearer_returns_token(self):
        assert _extract_token_from_handshake(["bearer.jwt-abc"]) == "jwt-abc"

    def test_single_bearer_among_unrelated_subprotocols(self):
        # The constant auth.v1 (or any non-bearer value) does not interfere.
        assert (
            _extract_token_from_handshake(["auth.v1", "bearer.jwt-abc"]) == "jwt-abc"
        )

    def test_single_bearer_order_does_not_matter(self):
        assert (
            _extract_token_from_handshake(["bearer.jwt-abc", "auth.v1"]) == "jwt-abc"
        )

    def test_token_with_embedded_dots_preserved(self):
        # A JWT-style token contains dots; only the prefix ``bearer.`` is split off.
        token = "eyJhbGci.eyJzdWIi.SflKxwRJ"
        assert _extract_token_from_handshake([f"bearer.{token}"]) == token

    def test_token_surrounding_whitespace_stripped(self):
        assert _extract_token_from_handshake(["bearer.  spaced-token  "]) == "spaced-token"

    def test_prefix_is_case_insensitive(self):
        # ``Bearer.``, ``BEARER.`` and ``bearer.`` are all valid prefixes.
        assert _extract_token_from_handshake(["Bearer.jwt"]) == "jwt"
        assert _extract_token_from_handshake(["BEARER.jwt"]) == "jwt"
        assert _extract_token_from_handshake(["BeArEr.jwt"]) == "jwt"


# ---------------------------------------------------------------------------
# No credential — returns None (not an error)
# ---------------------------------------------------------------------------


class TestExtractTokenNoCredential:
    def test_empty_list_returns_none(self):
        assert _extract_token_from_handshake([]) is None

    def test_none_returns_none(self):
        assert _extract_token_from_handshake(None) is None

    def test_only_constant_subprotocol_returns_none(self):
        # Offering the echo constant alone carries no credential.
        assert _extract_token_from_handshake(["auth.v1"]) is None

    def test_unrelated_subprotocols_return_none(self):
        assert _extract_token_from_handshake(["chat", "json", "v2"]) is None

    def test_bare_bearer_prefix_with_no_token_ignored(self):
        # ``bearer.`` with nothing after the dot is not a credential and is
        # ignored (returns None), not treated as ambiguous.
        assert _extract_token_from_handshake(["bearer."]) is None
        assert _extract_token_from_handshake(["bearer.   "]) is None

    def test_bare_bearer_does_not_trigger_ambiguity(self):
        # A bare ``bearer.`` next to a real token must still resolve cleanly.
        assert (
            _extract_token_from_handshake(["bearer.", "bearer.real-token"])
            == "real-token"
        )

    def test_non_string_entries_ignored(self):
        # Defensive: stray non-string entries must not crash the scan.
        assert _extract_token_from_handshake([None, 123, "auth.v1"]) is None  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Ambiguous multi-bearer handshakes — MUST be rejected
# ---------------------------------------------------------------------------


class TestExtractTokenAmbiguousRejection:
    def test_two_bearer_subprotocols_raises(self):
        with pytest.raises(AmbiguousSubprotocolError):
            _extract_token_from_handshake(["bearer.token-a", "bearer.token-b"])

    def test_three_bearer_subprotocols_raises(self):
        with pytest.raises(AmbiguousSubprotocolError):
            _extract_token_from_handshake(
                ["bearer.a", "bearer.b", "bearer.c", "auth.v1"]
            )

    def test_ambiguity_independent_of_other_subprotocols(self):
        # Unrelated subprotocols do not defuse the ambiguity.
        with pytest.raises(AmbiguousSubprotocolError):
            _extract_token_from_handshake(
                ["auth.v1", "bearer.token-a", "chat", "bearer.token-b"]
            )

    def test_bare_bearer_plus_two_real_tokens_still_ambiguous(self):
        # The bare ``bearer.`` is ignored, but two *real* tokens remain ambiguous.
        with pytest.raises(AmbiguousSubprotocolError):
            _extract_token_from_handshake(
                ["bearer.", "bearer.token-a", "bearer.token-b"]
            )

    def test_ambiguous_error_is_a_subclass_of_exception(self):
        # Callers that catch broad Exception must still catch it.
        with pytest.raises(Exception):  # noqa: B017
            _extract_token_from_handshake(["bearer.a", "bearer.b"])

    def test_ambiguous_error_message_mentions_bearer(self):
        # The error must be diagnosable from its message alone.
        with pytest.raises(AmbiguousSubprotocolError) as exc_info:
            _extract_token_from_handshake(["bearer.a", "bearer.b"])
        assert "bearer" in str(exc_info.value).lower()
        assert "ambiguous" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Raw header string handling (comma-separated Sec-WebSocket-Protocol value)
# ---------------------------------------------------------------------------


class TestExtractTokenRawHeader:
    def test_raw_header_single_bearer(self):
        assert _extract_token_from_handshake("bearer.jwt-from-header") == "jwt-from-header"

    def test_raw_header_with_constant(self):
        assert (
            _extract_token_from_handshake("bearer.jwt-from-header, auth.v1")
            == "jwt-from-header"
        )

    def test_raw_header_ambiguous_raises(self):
        with pytest.raises(AmbiguousSubprotocolError):
            _extract_token_from_handshake("bearer.a, bearer.b, auth.v1")

    def test_raw_header_no_bearer_returns_none(self):
        assert _extract_token_from_handshake("auth.v1, chat") is None

    def test_empty_string_header_returns_none(self):
        assert _extract_token_from_handshake("") is None


# ---------------------------------------------------------------------------
# Echo subprotocol selection — never echo the raw token
# ---------------------------------------------------------------------------


class _ScopelessWS:
    """A WebSocket double with a dict ``scope`` attribute."""


class TestSelectEchoSubprotocol:
    def test_echoes_constant_when_offered(self):
        ws = _ScopelessWS()
        ws.scope = {"subprotocols": ["bearer.jwt", WS_AUTH_SUBPROTOCOL]}
        assert select_echo_subprotocol(ws) == WS_AUTH_SUBPROTOCOL

    def test_echoes_constant_even_when_bearer_present(self):
        # The whole point: never echo the raw bearer token.
        ws = _ScopelessWS()
        ws.scope = {"subprotocols": ["bearer.super-secret-jwt", "auth.v1"]}
        echoed = select_echo_subprotocol(ws)
        assert echoed == WS_AUTH_SUBPROTOCOL
        assert "super-secret-jwt" not in (echoed or "")
        assert not (echoed or "").startswith(_BEARER_SUBPROTOCOL_PREFIX)

    def test_returns_none_when_constant_not_offered(self):
        # Query-param clients that never offered auth.v1 get no echo.
        ws = _ScopelessWS()
        ws.scope = {"subprotocols": ["bearer.jwt"]}
        assert select_echo_subprotocol(ws) is None

    def test_returns_none_when_no_subprotocols_offered(self):
        ws = _ScopelessWS()
        ws.scope = {"subprotocols": []}
        assert select_echo_subprotocol(ws) is None

    def test_returns_none_for_missing_scope(self):
        ws = _ScopelessWS()
        ws.scope = {}
        assert select_echo_subprotocol(ws) is None

    def test_returns_none_when_scope_not_a_dict(self):
        ws = _ScopelessWS()
        ws.scope = None  # type: ignore[assignment]
        assert select_echo_subprotocol(ws) is None

    def test_returns_none_when_scope_attr_missing(self):
        class _BareWS:
            pass

        assert select_echo_subprotocol(_BareWS()) is None  # type: ignore[arg-type]

    def test_raw_header_string_in_scope_supported(self):
        ws = _ScopelessWS()
        ws.scope = {"subprotocols": "bearer.jwt, auth.v1"}
        assert select_echo_subprotocol(ws) == WS_AUTH_SUBPROTOCOL

    def test_constant_value_is_token_free(self):
        # Sanity guard on the exported constant itself.
        assert WS_AUTH_SUBPROTOCOL == "auth.v1"
        assert _BEARER_SUBPROTOCOL_PREFIX not in WS_AUTH_SUBPROTOCOL
