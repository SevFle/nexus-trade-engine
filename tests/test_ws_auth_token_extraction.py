"""Comprehensive tests for the WebSocket token-extraction path.

Targets the recently-changed handshake token-extraction logic in
``engine/api/ws/auth.py`` and the two flows that build on it:

* ``_extract_token_from_handshake`` — the priority chain
  ``Authorization`` header → ``Sec-WebSocket-Protocol`` subprotocol →
  ``token`` query parameter.
* ``authenticate_websocket`` / ``validate_session_token_for_ws`` — which must
  return a proper ``AuthResult`` on the authenticated path, or a
  ``(close_code, reason)`` tuple on the rejected path. The query-param path
  in particular previously fell through to first-message auth and returned a
  bare ``invalid auth message`` tuple instead of an ``AuthResult``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from engine.api.ws.auth import (
    AuthResult,
    _extract_token_from_handshake,
    authenticate_websocket,
    validate_session_token_for_ws,
)
from engine.api.ws.protocol import (
    WS_CLOSE_AUTH_FORBIDDEN,
    WS_CLOSE_AUTH_INVALID,
    WS_CLOSE_AUTH_TIMEOUT,
    WS_CLOSE_LEGAL_REACCEPT,
)

USER_UUID = uuid.UUID("00000000-0000-0000-0000-00000000face")
LEGAL_PATCH = "engine.api.ws.auth.legal_service.get_pending_acceptances"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeHost:
    host: str


@dataclass
class _FakeUser:
    id: uuid.UUID
    is_active: bool = True


class _FakeWebSocket:
    """Minimal WebSocket stand-in mirroring the Starlette surface we use."""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, Any] | None = None,
        client_host: str | None = "1.2.3.4",
    ) -> None:
        self.headers = headers or {}
        # Starlette exposes QueryParams as a dict-like with ``.get()``; a plain
        # dict mirrors that contract. ``None`` is also accepted to exercise
        # the ``getattr`` fallback for objects lacking the attribute.
        self.query_params = query_params if query_params is not None else {}
        self.client = _FakeHost(client_host) if client_host is not None else None
        self._receive_json = AsyncMock()
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str]] = []

    async def receive_json(self):
        return await self._receive_json()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


def _token_data(token: str) -> dict[str, Any]:
    """Decode mock that maps the raw token to a distinct subject.

    Used to prove which credential (header / subprotocol / query / message)
    actually won the priority race.
    """
    return {"sub": f"user-for-{token}", "role": "admin", "type": "access"}


def _make_db(user: _FakeUser | None) -> Any:
    """Fake async session whose ``execute`` yields ``user`` (or None)."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db.execute.return_value = result
    return db


# ---------------------------------------------------------------------------
# _extract_token_from_handshake — direct unit tests
# ---------------------------------------------------------------------------


class TestExtractTokenFromHandshake:
    # --- Authorization header ---
    def test_bearer_header(self):
        ws = _FakeWebSocket(headers={"authorization": "Bearer hdr-tok"})
        assert _extract_token_from_handshake(ws) == "hdr-tok"

    def test_bearer_header_scheme_case_insensitive(self):
        ws = _FakeWebSocket(headers={"authorization": "BEARER hdr-tok"})
        assert _extract_token_from_handshake(ws) == "hdr-tok"

    def test_bearer_header_lowercased_scheme(self):
        ws = _FakeWebSocket(headers={"authorization": "bearer hdr-tok"})
        assert _extract_token_from_handshake(ws) == "hdr-tok"

    def test_bearer_header_trims_value_whitespace(self):
        ws = _FakeWebSocket(headers={"authorization": "Bearer   spaced  "})
        assert _extract_token_from_handshake(ws) == "spaced"

    def test_non_bearer_header_returns_none(self):
        ws = _FakeWebSocket(headers={"authorization": "Basic abc"})
        assert _extract_token_from_handshake(ws) is None

    def test_header_single_token_returns_none(self):
        # "Bearer" with no credential part is malformed.
        ws = _FakeWebSocket(headers={"authorization": "Bearer"})
        assert _extract_token_from_handshake(ws) is None

    def test_header_empty_token_returns_none(self):
        ws = _FakeWebSocket(headers={"authorization": "Bearer   "})
        assert _extract_token_from_handshake(ws) is None

    # --- Sec-WebSocket-Protocol subprotocol ---
    def test_subprotocol_bearer_prefixed(self):
        ws = _FakeWebSocket(headers={"sec-websocket-protocol": "bearer.sub-tok"})
        assert _extract_token_from_handshake(ws) == "sub-tok"

    def test_subprotocol_bearer_prefixed_picks_first_among_many(self):
        ws = _FakeWebSocket(headers={"sec-websocket-protocol": "chat, bearer.sub-tok, v1"})
        assert _extract_token_from_handshake(ws) == "sub-tok"

    def test_subprotocol_bare_single_value(self):
        ws = _FakeWebSocket(headers={"sec-websocket-protocol": "bare-tok"})
        assert _extract_token_from_handshake(ws) == "bare-tok"

    def test_subprotocol_multiple_non_bearer_is_ambiguous(self):
        ws = _FakeWebSocket(headers={"sec-websocket-protocol": "chat, v1"})
        assert _extract_token_from_handshake(ws) is None

    # --- Query parameter (lowest priority) ---
    def test_query_param_token(self):
        ws = _FakeWebSocket(query_params={"token": "qp-tok"})
        assert _extract_token_from_handshake(ws) == "qp-tok"

    def test_query_param_whitespace_stripped(self):
        ws = _FakeWebSocket(query_params={"token": "  qp-tok  "})
        assert _extract_token_from_handshake(ws) == "qp-tok"

    def test_query_param_empty_returns_none(self):
        ws = _FakeWebSocket(query_params={"token": ""})
        assert _extract_token_from_handshake(ws) is None

    def test_query_param_whitespace_only_returns_none(self):
        ws = _FakeWebSocket(query_params={"token": "   "})
        assert _extract_token_from_handshake(ws) is None

    def test_query_param_missing_key_returns_none(self):
        ws = _FakeWebSocket(query_params={"other": "x"})
        assert _extract_token_from_handshake(ws) is None

    def test_query_param_non_string_returns_none(self):
        ws = _FakeWebSocket(query_params={"token": 12345})  # type: ignore[dict-item]
        assert _extract_token_from_handshake(ws) is None

    def test_missing_query_params_attr_returns_none(self):
        ws = _FakeWebSocket()
        del ws.query_params
        assert _extract_token_from_handshake(ws) is None

    # --- Priority ordering ---
    def test_header_wins_over_subprotocol_and_query(self):
        ws = _FakeWebSocket(
            headers={
                "authorization": "Bearer hdr-tok",
                "sec-websocket-protocol": "bearer.sub-tok",
            },
            query_params={"token": "qp-tok"},
        )
        assert _extract_token_from_handshake(ws) == "hdr-tok"

    def test_subprotocol_wins_over_query(self):
        ws = _FakeWebSocket(
            headers={"sec-websocket-protocol": "bearer.sub-tok"},
            query_params={"token": "qp-tok"},
        )
        assert _extract_token_from_handshake(ws) == "sub-tok"

    def test_no_token_anywhere_returns_none(self):
        assert _extract_token_from_handshake(_FakeWebSocket()) is None


# ---------------------------------------------------------------------------
# authenticate_websocket — end-to-end credential paths & priority
# ---------------------------------------------------------------------------


class TestAuthenticateWebsocketPaths:
    @patch("engine.api.ws.auth.decode_token")
    async def test_header_token_authenticates(self, mock_decode):
        mock_decode.side_effect = _token_data
        ws = _FakeWebSocket(headers={"authorization": "Bearer hdr-tok"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-hdr-tok"
        mock_decode.assert_called_once_with("hdr-tok")

    @patch("engine.api.ws.auth.decode_token")
    async def test_subprotocol_token_authenticates(self, mock_decode):
        mock_decode.side_effect = _token_data
        ws = _FakeWebSocket(headers={"sec-websocket-protocol": "bearer.sub-tok"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-sub-tok"
        mock_decode.assert_called_once_with("sub-tok")

    @patch("engine.api.ws.auth.decode_token")
    async def test_query_param_token_authenticates(self, mock_decode):
        # The regression under test: a query-param credential must yield an
        # AuthResult, NOT a bare "invalid auth message" tuple.
        mock_decode.side_effect = _token_data
        ws = _FakeWebSocket(query_params={"token": "qp-tok"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-qp-tok"
        assert "read:portfolio:all" in result.scopes
        mock_decode.assert_called_once_with("qp-tok")

    @patch("engine.api.ws.auth.decode_token")
    async def test_query_param_token_skips_first_message(self, mock_decode):
        # When a handshake credential (query param) is present, the first JSON
        # message must NOT be awaited.
        mock_decode.side_effect = _token_data
        ws = _FakeWebSocket(query_params={"token": "qp-tok"})
        ws._receive_json.side_effect = AssertionError("must not await auth message")
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-qp-tok"

    @patch("engine.api.ws.auth.decode_token")
    async def test_priority_header_over_query(self, mock_decode):
        mock_decode.side_effect = _token_data
        ws = _FakeWebSocket(
            headers={"authorization": "Bearer hdr-tok"},
            query_params={"token": "qp-tok"},
        )
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-hdr-tok"

    @patch("engine.api.ws.auth.decode_token")
    async def test_priority_subprotocol_over_query(self, mock_decode):
        mock_decode.side_effect = _token_data
        ws = _FakeWebSocket(
            headers={"sec-websocket-protocol": "bearer.sub-tok"},
            query_params={"token": "qp-tok"},
        )
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-sub-tok"

    @patch("engine.api.ws.auth.decode_token", return_value=None)
    async def test_invalid_query_param_token_rejected(self, mock_decode):
        ws = _FakeWebSocket(query_params={"token": "bad"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_query_param_missing_sub_rejected(self, mock_decode):
        mock_decode.return_value = {"role": "admin", "type": "access"}
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_empty_query_param_falls_back_to_message(self, mock_decode):
        # An empty query token is ignored, so first-message auth is attempted.
        mock_decode.return_value = _token_data("msg-tok")
        ws = _FakeWebSocket(query_params={"token": ""})
        ws._receive_json.return_value = {"type": "auth", "token": "msg-tok"}
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-msg-tok"

    async def test_message_auth_still_works(self):
        # Backcompat: no handshake credential → first-message auth.
        with patch(
            "engine.api.ws.auth.decode_token", return_value=_token_data("msg-tok")
        ) as mock_decode:
            ws = _FakeWebSocket()
            ws._receive_json.return_value = {"type": "auth", "token": "msg-tok"}
            result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "user-for-msg-tok"
        mock_decode.assert_called_once_with("msg-tok")

    async def test_no_credential_times_out(self):
        ws = _FakeWebSocket()
        ws._receive_json.side_effect = TimeoutError()
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_TIMEOUT


# ---------------------------------------------------------------------------
# validate_session_token_for_ws — authenticated (AuthResult) vs rejected (tuple)
# ---------------------------------------------------------------------------


class TestValidateSessionTokenForWs:
    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_valid_token_returns_authresult(self, mock_decode, mock_legal):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        mock_legal.return_value = []
        result = await validate_session_token_for_ws(_make_db(_FakeUser(USER_UUID)), "good")
        assert isinstance(result, AuthResult)
        assert result.user_id == str(USER_UUID)
        assert "read:portfolio:all" in result.scopes

    @patch("engine.api.ws.auth.decode_token", return_value=None)
    async def test_undecodable_token_rejected(self, mock_decode):
        result = await validate_session_token_for_ws(_make_db(None), "bad")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_non_uuid_subject_rejected(self, mock_decode):
        mock_decode.return_value = {"sub": "not-a-uuid", "role": "admin", "type": "access"}
        result = await validate_session_token_for_ws(_make_db(None), "jwt")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_missing_subject_rejected(self, mock_decode):
        mock_decode.return_value = {"role": "admin", "type": "access"}
        result = await validate_session_token_for_ws(_make_db(None), "jwt")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_user_not_found_rejected(self, mock_decode):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        result = await validate_session_token_for_ws(_make_db(None), "jwt")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_disabled_user_rejected(self, mock_decode):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        db = _make_db(_FakeUser(USER_UUID, is_active=False))
        result = await validate_session_token_for_ws(db, "jwt")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_insufficient_scope_rejected(self, mock_decode, mock_legal):
        # viewer role → base read scopes; demanding an :all scope fails.
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "viewer",
            "type": "access",
        }
        mock_legal.return_value = []
        result = await validate_session_token_for_ws(
            _make_db(_FakeUser(USER_UUID)),
            "jwt",
            required_scopes=["read:portfolio:all"],
        )
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_FORBIDDEN

    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_required_scope_granted_returns_authresult(self, mock_decode, mock_legal):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        mock_legal.return_value = []
        result = await validate_session_token_for_ws(
            _make_db(_FakeUser(USER_UUID)),
            "jwt",
            required_scopes=["read:portfolio:all", "read:orders:all"],
        )
        assert isinstance(result, AuthResult)

    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_legal_reacceptance_required_rejected(self, mock_decode, mock_legal):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        mock_legal.return_value = ["terms-of-service-v2"]  # pending docs
        result = await validate_session_token_for_ws(_make_db(_FakeUser(USER_UUID)), "jwt")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_LEGAL_REACCEPT

    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_enforce_legal_false_skips_legal(self, mock_decode, mock_legal):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        mock_legal.return_value = ["pending"]  # would block, but enforcement off
        result = await validate_session_token_for_ws(
            _make_db(_FakeUser(USER_UUID)), "jwt", enforce_legal=False
        )
        assert isinstance(result, AuthResult)
        mock_legal.assert_not_called()

    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_legal_store_failure_fails_closed(self, mock_decode, mock_legal):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        mock_legal.side_effect = RuntimeError("legal store down")
        result = await validate_session_token_for_ws(_make_db(_FakeUser(USER_UUID)), "jwt")
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID


# ---------------------------------------------------------------------------
# authenticate_websocket — db-backed path returns AuthResult, not a raw tuple
# ---------------------------------------------------------------------------


class TestAuthenticateWebsocketDbPath:
    @patch(LEGAL_PATCH, new_callable=AsyncMock)
    @patch("engine.api.ws.auth.decode_token")
    async def test_db_path_query_param_returns_authresult(self, mock_decode, mock_legal):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        mock_legal.return_value = []
        ws = _FakeWebSocket(query_params={"token": "good"})
        result = await authenticate_websocket(ws, db=_make_db(_FakeUser(USER_UUID)))
        assert isinstance(result, AuthResult)
        assert result.user_id == str(USER_UUID)

    @patch("engine.api.ws.auth.decode_token")
    async def test_db_path_revoked_user_returns_tuple(self, mock_decode):
        mock_decode.return_value = {
            "sub": str(USER_UUID),
            "role": "admin",
            "type": "access",
        }
        ws = _FakeWebSocket(query_params={"token": "good"})
        result = await authenticate_websocket(ws, db=_make_db(None))
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token", return_value=None)
    async def test_db_path_invalid_token_returns_tuple(self, mock_decode):
        ws = _FakeWebSocket(headers={"authorization": "Bearer bad"})
        result = await authenticate_websocket(ws, db=_make_db(_FakeUser(USER_UUID)))
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID
