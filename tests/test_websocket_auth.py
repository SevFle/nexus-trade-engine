"""Unit tests for engine.api.websocket.auth (SEV-275).

These tests stub out the DB session factory so we exercise the
extraction / scope-resolution logic without spinning up Postgres.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from engine.api.websocket import auth as auth_mod
from engine.api.websocket.auth import (
    CHANNEL_REQUIRED_SCOPE,
    _extract_authorization_header,
    _extract_query_token,
    _extract_subprotocol,
    _extract_token,
    _scopes_for_role,
    authorize_channel,
    close_code_for,
)
from engine.api.websocket.constants import CloseCode
from engine.api.websocket.exceptions import (
    AuthRequiredError,
    ForbiddenError,
    InvalidTokenError,
)
from engine.api.websocket.models import Principal


class _FakeWS:
    def __init__(
        self,
        *,
        subprotocol: str | None = None,
        auth_header: str | None = None,
        query_token: str | None = None,
    ) -> None:
        self.scope: dict = {}
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        if subprotocol:
            self.scope["subprotocols"] = [subprotocol]
        if auth_header:
            self.headers["authorization"] = auth_header
        if query_token:
            self.query_params["token"] = query_token


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------
class TestSubprotocol:
    def test_extracts_nexus_prefixed(self):
        ws = _FakeWS(subprotocol="nexus.jwt_payload")
        assert _extract_subprotocol(ws) == "jwt_payload"

    def test_rejects_other_prefix(self):
        ws = _FakeWS(subprotocol="other.token")
        assert _extract_subprotocol(ws) is None

    def test_missing_returns_none(self):
        assert _extract_subprotocol(_FakeWS()) is None


class TestHeader:
    def test_extracts_bearer(self):
        ws = _FakeWS(auth_header="Bearer abc.def")
        assert _extract_authorization_header(ws) == "abc.def"

    def test_case_insensitive_scheme(self):
        ws = _FakeWS(auth_header="bearer abc")
        assert _extract_authorization_header(ws) == "abc"

    def test_rejects_non_bearer(self):
        ws = _FakeWS(auth_header="Basic abc")
        assert _extract_authorization_header(ws) is None

    def test_empty_token_returns_none(self):
        ws = _FakeWS(auth_header="Bearer   ")
        assert _extract_authorization_header(ws) is None


class TestQuery:
    def test_extracts_token(self):
        assert _extract_query_token(_FakeWS(query_token="abc")) == "abc"

    def test_strips_whitespace(self):
        assert _extract_query_token(_FakeWS(query_token="  abc  ")) == "abc"

    def test_missing_returns_none(self):
        assert _extract_query_token(_FakeWS()) is None


class TestExtractionOrder:
    def test_subprotocol_wins_over_header_and_query(self):
        ws = _FakeWS(
            subprotocol="nexus.from_subprotocol",
            auth_header="Bearer from_header",
            query_token="from_query",
        )
        token, method = _extract_token(ws)  # type: ignore[misc]
        assert token == "from_subprotocol"
        assert method == "subprotocol"

    def test_header_beats_query(self):
        ws = _FakeWS(auth_header="Bearer from_header", query_token="from_query")
        token, method = _extract_token(ws)  # type: ignore[misc]
        assert token == "from_header"
        assert method == "header"

    def test_query_is_last_resort(self):
        ws = _FakeWS(query_token="from_query")
        token, method = _extract_token(ws)  # type: ignore[misc]
        assert token == "from_query"
        assert method == "query"

    def test_all_missing_returns_none(self):
        assert _extract_token(_FakeWS()) is None


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------
class TestScopes:
    def test_user_role_gets_read_scopes(self):
        scopes = _scopes_for_role("user")
        assert "portfolio:read" in scopes
        assert "orders:read" in scopes
        assert "market:read" in scopes
        assert "admin" not in scopes

    def test_admin_role_implies_everything(self):
        scopes = _scopes_for_role("admin")
        assert "admin" in scopes

    def test_api_key_admin_scope_grants_admin(self):
        scopes = _scopes_for_role("user", api_key_scopes=["admin"])
        assert "admin" in scopes

    def test_api_key_read_expands_to_read_scopes(self):
        scopes = _scopes_for_role("user", api_key_scopes=["read"])
        assert "portfolio:read" in scopes
        assert "orders:read" in scopes


# ---------------------------------------------------------------------------
# authorize_channel
# ---------------------------------------------------------------------------
class TestAuthorizeChannel:
    def _principal(self, scopes: set[str]) -> Principal:
        return Principal(
            user_id=uuid.uuid4(),
            email="t@example.com",
            role="user",
            scopes=frozenset(scopes),
        )

    def test_allowed_scope_passes(self):
        authorize_channel(
            self._principal({"portfolio:read"}),
            "portfolio",
        )

    def test_missing_scope_raises(self):
        with pytest.raises(ForbiddenError):
            authorize_channel(
                self._principal({"market:read"}),
                "portfolio",
            )

    def test_admin_implies_all(self):
        authorize_channel(self._principal({"admin"}), "portfolio")
        authorize_channel(self._principal({"admin"}), "orders")
        authorize_channel(self._principal({"admin"}), "market")
        authorize_channel(self._principal({"admin"}), "market_depth")

    def test_unknown_channel_passes(self):
        authorize_channel(self._principal(set()), "wizardry")

    def test_required_scope_table_is_consistent(self):
        for ch, scope in CHANNEL_REQUIRED_SCOPE.items():
            assert isinstance(ch, str)
            assert isinstance(scope, str)


# ---------------------------------------------------------------------------
# close_code_for
# ---------------------------------------------------------------------------
class TestCloseCodeFor:
    def test_websocket_error_uses_its_code(self):
        e = InvalidTokenError()
        assert close_code_for(e) == CloseCode.AUTH_FAILED

    def test_unknown_exception_falls_back_to_internal_error(self):
        assert close_code_for(RuntimeError("boom")) == CloseCode.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# authenticate()
# ---------------------------------------------------------------------------
class TestAuthenticate:
    @pytest.fixture
    def stub_session_factory(self, monkeypatch):
        """Replace get_session_factory with a fake that yields a
        session whose scalar_one_or_none returns a stub User."""

        @asynccontextmanager
        async def factory():
            yield MagicMock()

        monkeypatch.setattr(auth_mod, "get_session_factory", lambda: factory)
        return factory

    async def test_missing_token_raises_auth_required(self, stub_session_factory):
        with pytest.raises(AuthRequiredError):
            await auth_mod.authenticate(_FakeWS())
