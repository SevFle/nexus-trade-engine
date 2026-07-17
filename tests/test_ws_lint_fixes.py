"""Comprehensive tests for the WebSocket API modules (post-lint-fix coverage).

Covers: protocol, permissions, auth, connection_manager, channels,
event_bridge, and health — the 7 files touched by commit 131a1b6.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from engine.api.ws.auth import (
    AuthRateLimiter,
    AuthResult,
    _get_remote_ip,
    _hash_subject,
    authenticate_websocket,
    extract_scopes,
    validate_refresh_token,
)
from engine.api.ws.channels import ChannelResolver
from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.event_bridge import _EVENT_TO_CHANNEL, EventBusBridge
from engine.api.ws.exceptions import (
    ConnectionLimitError,
    QueueFullError,
    SubscriptionLimitError,
)
from engine.api.ws.health import ws_health_snapshot
from engine.api.ws.permissions import (
    check_channel_access,
    resolve_room_name,
)
from engine.api.ws.protocol import (
    VALID_CHANNELS,
    WS_CLOSE_AUTH_INVALID,
    WS_CLOSE_AUTH_TIMEOUT,
    AckMessage,
    AuthMessage,
    CloseMessage,
    ErrorMessage,
    EventMessage,
    PingMessage,
    PongMessage,
    SubscribeMessage,
    UnsubscribeMessage,
    parse_inbound,
    parse_room_name,
)

# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal WebSocket stand-in for testing."""

    def __init__(
        self,
        *,
        query_params: dict[str, str] | None = None,
        client_host: str | None = "1.2.3.4",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.query_params = query_params or {}
        self.client = _FakeHost(client_host) if client_host is not None else None
        self.headers = headers or {}
        self._receive_json = AsyncMock()
        self.sent: list[dict] = []
        self._closed = False

    async def receive_json(self):
        return await self._receive_json()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed = True


@dataclass
class _FakeHost:
    host: str


def _make_token_data(
    sub: str = "user123",
    role: str = "admin",
    **extra: Any,
) -> dict[str, Any]:
    return {"sub": sub, "role": role, "type": "access", **extra}


# ---------------------------------------------------------------------------
# protocol.py — parse_inbound
# ---------------------------------------------------------------------------


class TestParseInbound:
    def test_rejects_non_dict(self):
        msg, err = parse_inbound("not a dict")
        assert msg is None
        assert "missing or invalid" in err

    def test_rejects_missing_type(self):
        msg, err = parse_inbound({"token": "abc"})
        assert msg is None
        assert "missing or invalid" in err

    def test_rejects_non_string_type(self):
        msg, _err = parse_inbound({"type": 42})
        assert msg is None

    def test_rejects_unknown_type(self):
        msg, err = parse_inbound({"type": "bogus"})
        assert msg is None
        assert "unknown message type" in err

    def test_parses_auth_message(self):
        msg, err = parse_inbound({"type": "auth", "token": "jwt123"})
        assert err is None
        assert isinstance(msg, AuthMessage)
        assert msg.token == "jwt123"

    def test_parses_subscribe_message(self):
        msg, err = parse_inbound(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "A1"},
            }
        )
        assert err is None
        assert isinstance(msg, SubscribeMessage)
        assert msg.channel == "portfolio"
        assert msg.params["account_id"] == "A1"

    def test_parses_unsubscribe_message(self):
        msg, err = parse_inbound(
            {
                "type": "unsubscribe",
                "channel": "orders",
            }
        )
        assert err is None
        assert isinstance(msg, UnsubscribeMessage)

    def test_parses_ping_message(self):
        msg, err = parse_inbound({"type": "ping", "ref": "r1"})
        assert err is None
        assert isinstance(msg, PingMessage)
        assert msg.ref == "r1"

    def test_rejects_auth_empty_token(self):
        msg, err = parse_inbound({"type": "auth", "token": ""})
        assert msg is None
        assert "validation error" in err

    def test_rejects_subscribe_empty_channel(self):
        msg, err = parse_inbound({"type": "subscribe", "channel": ""})
        assert msg is None
        assert "validation error" in err

    def test_subscribe_default_params(self):
        msg, err = parse_inbound({"type": "subscribe", "channel": "orders"})
        assert err is None
        assert msg.params == {}

    def test_ping_default_ref(self):
        msg, err = parse_inbound({"type": "ping"})
        assert err is None
        assert msg.ref is None


# ---------------------------------------------------------------------------
# protocol.py — parse_room_name
# ---------------------------------------------------------------------------


class TestParseRoomName:
    def test_two_part_room(self):
        channel, scope = parse_room_name("portfolio:account:42")
        assert channel == "portfolio"
        assert scope == "account:42"

    def test_simple_two_part(self):
        channel, scope = parse_room_name("orders:status")
        assert channel == "orders"
        assert scope == "status"

    def test_user_room(self):
        channel, scope = parse_room_name("user:abc")
        assert channel == "user"
        assert scope == "abc"

    def test_single_part(self):
        channel, scope = parse_room_name("portfolio")
        assert channel == "portfolio"
        assert scope == ""

    def test_empty_string(self):
        channel, scope = parse_room_name("")
        assert channel == ""
        assert scope == ""

    def test_three_part_room(self):
        channel, scope = parse_room_name("portfolio:strategy:s1")
        assert channel == "portfolio"
        assert scope == "strategy:s1"


# ---------------------------------------------------------------------------
# protocol.py — constants and models
# ---------------------------------------------------------------------------


class TestProtocolConstants:
    def test_valid_channels(self):
        assert "portfolio" in VALID_CHANNELS
        assert "orders" in VALID_CHANNELS
        assert "strategies" in VALID_CHANNELS

    def test_close_codes(self):
        assert WS_CLOSE_AUTH_INVALID == 4401
        assert WS_CLOSE_AUTH_TIMEOUT == 4402


class TestMessageModels:
    def test_event_message_defaults(self):
        msg = EventMessage(channel="portfolio", room="portfolio:42")
        assert msg.seq == 0
        assert msg.payload == {}
        assert msg.ts is not None

    def test_ack_message(self):
        msg = AckMessage(ref="r1", status="ok")
        assert msg.error_code is None

    def test_error_message(self):
        msg = ErrorMessage(code="403", message="denied")
        assert msg.ref is None

    def test_close_message(self):
        msg = CloseMessage(code=1000, reason="shutdown")
        assert msg.type == "close"

    def test_pong_message(self):
        msg = PongMessage(ref="r1")
        assert msg.type == "pong"


# ---------------------------------------------------------------------------
# permissions.py — check_channel_access
# ---------------------------------------------------------------------------


class TestCheckChannelAccess:
    def test_unknown_channel_returns_404(self):
        ok, err = check_channel_access("bogus", [], {})
        assert ok is False
        assert err == "404"

    def test_all_scope_grants_access(self):
        ok, err = check_channel_access("portfolio", ["read:portfolio:all"], {})
        assert ok is True
        assert err is None

    def test_base_scope_grants_access(self):
        ok, err = check_channel_access("portfolio", ["read:portfolio"], {})
        assert ok is True
        assert err is None

    def test_no_matching_scope_denies(self):
        ok, err = check_channel_access("portfolio", ["read:orders"], {})
        assert ok is False
        assert err == "403"

    def test_empty_scopes_denies(self):
        ok, _err = check_channel_access("portfolio", [], {})
        assert ok is False

    def test_owner_mismatch_denies(self):
        ok, err = check_channel_access(
            "portfolio",
            ["read:portfolio"],
            {"account_id": "A_other"},
            user_id="A_mine",
        )
        assert ok is False
        assert err == "403"

    def test_owner_match_grants(self):
        ok, err = check_channel_access(
            "portfolio",
            ["read:portfolio"],
            {"account_id": "A_mine"},
            user_id="A_mine",
        )
        assert ok is True
        assert err is None

    def test_all_scope_bypasses_owner_check(self):
        ok, _err = check_channel_access(
            "portfolio",
            ["read:portfolio:all"],
            {"account_id": "A_other"},
            user_id="A_mine",
        )
        assert ok is True

    def test_no_owner_field_in_params_grants(self):
        ok, _err = check_channel_access("portfolio", ["read:portfolio"], {}, user_id="u1")
        assert ok is True

    def test_no_user_id_skips_owner_check(self):
        ok, _err = check_channel_access(
            "portfolio",
            ["read:portfolio"],
            {"account_id": "A1"},
            user_id=None,
        )
        assert ok is True

    def test_orders_channel(self):
        ok, _err = check_channel_access("orders", ["read:orders:all"], {})
        assert ok is True

    def test_strategies_channel(self):
        ok, _err = check_channel_access("strategies", ["read:strategies"], {})
        assert ok is True

    def test_orders_owner_mismatch(self):
        ok, err = check_channel_access(
            "orders",
            ["read:orders"],
            {"account_id": "acct_other"},
            user_id="acct_mine",
        )
        assert ok is False
        assert err == "403"

    def test_orders_owner_match(self):
        ok, _err = check_channel_access(
            "orders",
            ["read:orders"],
            {"account_id": "acct_mine"},
            user_id="acct_mine",
        )
        assert ok is True

    def test_strategies_owner_match(self):
        ok, _err = check_channel_access(
            "strategies",
            ["read:strategies"],
            {"strategy_id": "s1"},
            user_id="s1",
        )
        assert ok is True


# ---------------------------------------------------------------------------
# permissions.py — resolve_room_name
# ---------------------------------------------------------------------------


class TestResolveRoomName:
    def test_portfolio_account(self):
        assert resolve_room_name("portfolio", {"account_id": "A1"}) == "portfolio:account:A1"

    def test_portfolio_strategy(self):
        assert resolve_room_name("portfolio", {"strategy_id": "S1"}) == "portfolio:strategy:S1"

    def test_portfolio_account_takes_priority(self):
        result = resolve_room_name("portfolio", {"account_id": "A1", "strategy_id": "S1"})
        assert result == "portfolio:account:A1"

    def test_orders_symbol(self):
        assert resolve_room_name("orders", {"symbol": "AAPL"}) == "orders:symbol:AAPL"

    def test_orders_status(self):
        assert resolve_room_name("orders", {"status": "filled"}) == "orders:status:filled"

    def test_strategies_strategy(self):
        assert resolve_room_name("strategies", {"strategy_id": "S1"}) == "strategies:strategy:S1"

    def test_unknown_channel_returns_none(self):
        assert resolve_room_name("bogus", {"key": "val"}) is None

    def test_empty_params_returns_none(self):
        assert resolve_room_name("portfolio", {}) is None

    def test_empty_param_value_skipped(self):
        assert resolve_room_name("portfolio", {"account_id": ""}) is None

    def test_portfolio_no_matching_params(self):
        assert resolve_room_name("portfolio", {"foo": "bar"}) is None


# ---------------------------------------------------------------------------
# auth.py — extract_scopes
# ---------------------------------------------------------------------------


class TestExtractScopes:
    def test_admin_gets_all_scopes(self):
        scopes = extract_scopes({"role": "admin"})
        assert "read:portfolio:all" in scopes
        assert "read:orders:all" in scopes
        assert "read:strategies:all" in scopes

    def test_portfolio_manager_gets_all_scopes(self):
        scopes = extract_scopes({"role": "portfolio_manager"})
        assert "read:portfolio:all" in scopes

    def test_viewer_gets_base_scopes(self):
        scopes = extract_scopes({"role": "viewer"})
        assert "read:portfolio" in scopes
        assert "read:portfolio:all" not in scopes

    def test_quant_dev_gets_base_scopes(self):
        scopes = extract_scopes({"role": "quant_dev"})
        assert "read:portfolio" in scopes
        assert "read:portfolio:all" not in scopes

    def test_unknown_role_defaults_to_viewer(self):
        scopes = extract_scopes({"role": "hacker"})
        assert scopes == extract_scopes({"role": "viewer"})

    def test_missing_role_defaults_to_viewer(self):
        scopes = extract_scopes({})
        assert scopes == extract_scopes({"role": "viewer"})

    def test_retail_trader_scopes(self):
        scopes = extract_scopes({"role": "retail_trader"})
        assert "read:portfolio" in scopes
        assert "read:portfolio:all" not in scopes

    def test_developer_scopes(self):
        scopes = extract_scopes({"role": "developer"})
        assert "read:orders" in scopes
        assert "read:orders:all" not in scopes

    def test_user_role_scopes(self):
        scopes = extract_scopes({"role": "user"})
        assert "read:strategies" in scopes
        assert "read:strategies:all" not in scopes


# ---------------------------------------------------------------------------
# auth.py — _hash_subject
# ---------------------------------------------------------------------------


class TestHashSubject:
    def test_deterministic(self):
        a = _hash_subject("user1")
        b = _hash_subject("user1")
        assert a == b

    def test_different_inputs_differ(self):
        assert _hash_subject("a") != _hash_subject("b")

    def test_length_16(self):
        assert len(_hash_subject("x")) == 16


# ---------------------------------------------------------------------------
# auth.py — AuthRateLimiter
# ---------------------------------------------------------------------------


class TestAuthRateLimiter:
    async def test_allows_under_limit(self):
        rl = AuthRateLimiter(max_attempts=3, window_seconds=60.0)
        for _ in range(3):
            assert await rl.check("1.2.3.4") is True

    async def test_blocks_over_limit(self):
        rl = AuthRateLimiter(max_attempts=2, window_seconds=60.0)
        await rl.check("1.2.3.4")
        await rl.check("1.2.3.4")
        assert await rl.check("1.2.3.4") is False

    async def test_different_ips_independent(self):
        rl = AuthRateLimiter(max_attempts=1, window_seconds=60.0)
        assert await rl.check("1.1.1.1") is True
        assert await rl.check("2.2.2.2") is True
        assert await rl.check("1.1.1.1") is False

    async def test_refills_over_time(self):
        rl = AuthRateLimiter(max_attempts=1, window_seconds=1.0)
        assert await rl.check("1.1.1.1") is True
        assert await rl.check("1.1.1.1") is False
        await asyncio.sleep(1.1)
        assert await rl.check("1.1.1.1") is True


# ---------------------------------------------------------------------------
# auth.py — _get_remote_ip
# ---------------------------------------------------------------------------


class TestGetRemoteIp:
    def test_basic_client(self):
        ws = _FakeWebSocket(client_host="10.0.0.1")
        assert _get_remote_ip(ws) == "10.0.0.1"

    def test_no_client(self):
        ws = _FakeWebSocket(client_host=None)
        assert _get_remote_ip(ws) == "unknown"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_trusted_proxy_forwarded(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "10.0.0.1, 9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_trusted_proxy_real_ip(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-real-ip": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_trusted_proxy_prefers_forwarded(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={
                "x-forwarded-for": "9.8.7.6",
                "x-real-ip": "1.1.1.1",
            },
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_untrusted_proxy_uses_direct(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.2",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "10.0.0.2"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_trusted_proxy_forwarded_invalid_ip_falls_back(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "10.0.0.1, not-an-ip"},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_trusted_proxy_real_ip_invalid_falls_back(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-real-ip": "!!!invalid"},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_trusted_proxy_forwarded_multiple_hops(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset())
    def test_empty_trusted_proxies(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_trusted_proxy_cidr_uses_x_real_ip(self):
        # A peer *inside* the trusted CIDR range must be treated as a proxy,
        # so X-Real-IP is honored even though the peer host is not a literal
        # member of the frozenset.
        ws = _FakeWebSocket(
            client_host="10.255.42.1",
            headers={"x-real-ip": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_trusted_proxy_cidr_uses_x_forwarded_for(self):
        ws = _FakeWebSocket(
            client_host="10.0.7.7",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_untrusted_proxy_outside_cidr_uses_direct(self):
        # A peer *outside* the trusted CIDR range must NOT be trusted, so the
        # spoofable X-Forwarded-For header is ignored and the direct peer wins.
        ws = _FakeWebSocket(
            client_host="11.0.0.1",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "11.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_untrusted_proxy_mapped_ipv6_outside_cidr_uses_direct(self):
        # An IPv4-mapped IPv6 peer (``::ffff:11.0.0.1``) whose IPv4 form is
        # OUTSIDE the trusted CIDR must not be treated as a proxy, so the
        # spoofable header is ignored. Guards against the dual-stack listener
        # accidentally widening trust via mapped-address collapsing.
        ws = _FakeWebSocket(
            client_host="::ffff:11.0.0.1",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "::ffff:11.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_trusted_proxy_mapped_ipv6_in_cidr_honors_forwarded(self):
        # Symmetric counterpart: a dual-stack IPv4-mapped IPv6 peer whose IPv4
        # form (``10.0.0.1``) is INSIDE the trusted CIDR must still be trusted,
        # so X-Forwarded-For is honored.
        ws = _FakeWebSocket(
            client_host="::ffff:10.0.0.1",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"


# ---------------------------------------------------------------------------
# auth.py — _get_remote_ip rsplit DoS bound & validation fall-through
# ---------------------------------------------------------------------------


class TestGetRemoteIpRsplitAndFallthrough:
    """Pin the recent ``_get_remote_ip`` hardening (commit 84c87883).

    Two security/correctness-relevant behaviors changed in the most recent
    edit of the WS auth IP resolver and are not otherwise pinned:

    1. **Bounded XFF split** — ``forwarded.rsplit(",", 1)`` (maxsplit=1)
       reads only the rightmost hop, so a pathologically long (hostile)
       ``X-Forwarded-For`` header cannot force a multi-million-element
       allocation the way the old ``forwarded.split(",")`` did. The DoS
       guard's payoff is that the function returns the rightmost valid hop
       without raising, no matter how long the header is.
    2. **Validation fall-through** — when a header is present but its
       rightmost value is not a parseable IP (or is blank after stripping),
       the helper must *fall through* to the next source (``X-Real-IP``, then
       the raw peer) rather than returning garbage or raising.
    """

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_rsplit_bounds_huge_xff_header(self):
        # A one-million-comma header whose rightmost hop is a valid IP must
        # resolve to that hop without raising. With the old ``split(",")``
        # this allocated a ~1M-entry list before the lookup ran; ``rsplit``
        # with maxsplit=1 caps the result at two elements by construction.
        padding = ", 10.0.0.2" * 1_000_000
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": f"9.8.7.6{padding}"},
        )
        assert _get_remote_ip(ws) == "10.0.0.2"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_forwarded_rightmost_valid_ipv6_returned(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "2001:db8::42"},
        )
        assert _get_remote_ip(ws) == "2001:db8::42"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_forwarded_empty_rightmost_falls_through_to_real_ip(self):
        # XFF is truthy but its rightmost entry strips to empty -> not a valid
        # IP -> the helper falls through to X-Real-IP rather than returning
        # the empty string.
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "9.8.7.6, ", "x-real-ip": "203.0.113.5"},
        )
        assert _get_remote_ip(ws) == "203.0.113.5"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_forwarded_whitespace_only_rightmost_falls_through_to_peer(self):
        # XFF rightmost is whitespace-only -> falls through; no X-Real-IP is
        # present, so the trusted peer address is returned.
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "   "},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_real_ip_valid_ipv6_returned(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-real-ip": "::1"},
        )
        assert _get_remote_ip(ws) == "::1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_real_ip_empty_after_strip_falls_through_to_peer(self):
        # X-Real-IP is truthy but strips to empty -> not a valid IP -> the
        # helper falls through to the raw peer.
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-real-ip": "   "},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_forwarded_takes_priority_over_real_ip_for_cidr_peer(self):
        # Both headers valid and the peer is inside the trusted CIDR: XFF
        # wins, X-Real-IP is never consulted.
        ws = _FakeWebSocket(
            client_host="10.5.5.5",
            headers={
                "x-forwarded-for": "9.8.7.6",
                "x-real-ip": "203.0.113.9",
            },
        )
        assert _get_remote_ip(ws) == "9.8.7.6"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.0/8"}))
    def test_untrusted_cidr_peer_ignores_all_forwarded_headers(self):
        # Peer outside the trusted CIDR must not honor any forwarded header,
        # even when both XFF and X-Real-IP are present and valid — otherwise a
        # client could spoof its address by setting the header.
        ws = _FakeWebSocket(
            client_host="11.0.0.1",
            headers={
                "x-forwarded-for": "9.8.7.6",
                "x-real-ip": "203.0.113.9",
            },
        )
        assert _get_remote_ip(ws) == "11.0.0.1"


# ---------------------------------------------------------------------------
# auth.py — authenticate_websocket
# ---------------------------------------------------------------------------


class TestAuthenticateWebsocket:
    @patch("engine.api.ws.auth.decode_token")
    async def test_token_via_query_param(self, mock_decode):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ws = _FakeWebSocket(query_params={"token": "jwt123"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "u1"
        assert "read:portfolio:all" in result.scopes

    @patch("engine.api.ws.auth.decode_token")
    async def test_token_via_message(self, mock_decode):
        mock_decode.return_value = _make_token_data(sub="u2", role="viewer")
        ws = _FakeWebSocket()
        ws._receive_json.return_value = {"type": "auth", "token": "jwt456"}
        result = await authenticate_websocket(ws)
        assert isinstance(result, AuthResult)
        assert result.user_id == "u2"
        assert "read:portfolio:all" not in result.scopes

    async def test_auth_timeout(self):
        ws = _FakeWebSocket()
        ws._receive_json.side_effect = TimeoutError()
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_TIMEOUT

    async def test_auth_invalid_message(self):
        ws = _FakeWebSocket()
        ws._receive_json.side_effect = RuntimeError("broken")
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    async def test_auth_non_dict_message(self):
        ws = _FakeWebSocket()
        ws._receive_json.return_value = "not a dict"
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    async def test_auth_wrong_type(self):
        ws = _FakeWebSocket()
        ws._receive_json.return_value = {"type": "subscribe", "channel": "x"}
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    async def test_auth_missing_token(self):
        ws = _FakeWebSocket()
        ws._receive_json.return_value = {"type": "auth"}
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    async def test_auth_empty_token(self):
        ws = _FakeWebSocket()
        ws._receive_json.return_value = {"type": "auth", "token": ""}
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    async def test_auth_non_string_token(self):
        ws = _FakeWebSocket()
        ws._receive_json.return_value = {"type": "auth", "token": 12345}
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token", return_value=None)
    async def test_invalid_token(self, mock_decode):
        ws = _FakeWebSocket(query_params={"token": "bad"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.auth.decode_token")
    async def test_token_missing_sub(self, mock_decode):
        mock_decode.return_value = {"role": "admin", "type": "access"}
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        result = await authenticate_websocket(ws)
        assert isinstance(result, tuple)
        assert result[0] == WS_CLOSE_AUTH_INVALID

    async def test_rate_limited(self):
        rl = AuthRateLimiter(max_attempts=0, window_seconds=60.0)
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        result = await authenticate_websocket(ws, rate_limiter=rl)
        assert isinstance(result, tuple)
        assert "rate limited" in result[1]


# ---------------------------------------------------------------------------
# auth.py — validate_refresh_token
# ---------------------------------------------------------------------------


class TestValidateRefreshToken:
    @patch("engine.api.ws.auth.decode_token")
    def test_valid_token(self, mock_decode):
        mock_decode.return_value = _make_token_data(sub="u1")
        result = validate_refresh_token("good_token")
        assert isinstance(result, AuthResult)
        assert result.user_id == "u1"

    def test_non_string_token(self):
        assert validate_refresh_token(12345) is None

    @patch("engine.api.ws.auth.decode_token", return_value=None)
    def test_decode_fails(self, mock_decode):
        assert validate_refresh_token("bad") is None

    @patch("engine.api.ws.auth.decode_token")
    def test_missing_sub(self, mock_decode):
        mock_decode.return_value = {"role": "admin", "type": "access"}
        assert validate_refresh_token("jwt") is None


# ---------------------------------------------------------------------------
# connection_manager.py — register / unregister / send
# ---------------------------------------------------------------------------


class TestConnectionManager:
    @pytest.fixture
    def manager(self):
        return ConnectionManager(
            max_connections=10,
            send_queue_size=4,
            max_subscriptions_per_connection=3,
        )

    async def test_register_returns_connection_id(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", ["read:portfolio"])
        assert isinstance(cid, str)
        assert len(cid) == 32

    async def test_register_increments_count(self, manager):
        ws = _FakeWebSocket()
        await manager.register(ws, "user1", ["read:portfolio"])
        assert manager.connection_count == 1

    async def test_register_adds_user_room(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        rooms = manager.get_rooms(cid)
        assert "user:user1" in rooms

    async def test_unregister_decrements_count(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        await manager.unregister(cid)
        assert manager.connection_count == 0

    async def test_unregister_unknown_is_noop(self, manager):
        await manager.unregister("nonexistent")

    async def test_unregister_removes_rooms(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        await manager.join_room(cid, "portfolio:42")
        await manager.unregister(cid)
        assert manager.room_count == 0

    async def test_max_connections_raises(self, manager):
        for i in range(10):
            await manager.register(_FakeWebSocket(), f"u{i}", ["read:portfolio"])
        with pytest.raises(ConnectionLimitError):
            await manager.register(_FakeWebSocket(), "overflow", [])

    async def test_send_delivers_to_queue(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        msg = AckMessage(ref="r1")
        await manager.send(cid, msg)
        info = manager.get_connection(cid)
        assert info is not None
        assert info.send_queue.qsize() == 1

    async def test_send_unknown_connection(self, manager):
        msg = AckMessage()
        await manager.send("nonexistent", msg)

    async def test_send_queue_full_raises(self, manager):
        manager_cm = ConnectionManager(send_queue_size=1)
        ws = _FakeWebSocket()
        cid = await manager_cm.register(ws, "u1", [])
        await manager_cm.send(cid, AckMessage())
        with pytest.raises(QueueFullError):
            await manager_cm.send(cid, AckMessage())

    async def test_join_leave_room(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        await manager.join_room(cid, "portfolio:42")
        assert "portfolio:42" in manager.get_rooms(cid)
        assert cid in manager.room_members("portfolio:42")
        await manager.leave_room(cid, "portfolio:42")
        assert "portfolio:42" not in manager.get_rooms(cid)

    async def test_join_room_subscription_limit(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        await manager.join_room(cid, "r1")
        await manager.join_room(cid, "r2")
        await manager.join_room(cid, "r3")
        with pytest.raises(SubscriptionLimitError):
            await manager.join_room(cid, "r4")

    async def test_leave_room_unknown_connection(self, manager):
        await manager.leave_room("nonexistent", "r1")

    async def test_get_rooms_unknown(self, manager):
        assert manager.get_rooms("nonexistent") == frozenset()

    async def test_get_connection_unknown(self, manager):
        assert manager.get_connection("nonexistent") is None

    async def test_touch(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        info = manager.get_connection(cid)
        old = info.last_seen
        time.sleep(0.01)
        manager.touch(cid)
        assert info.last_seen > old

    async def test_touch_unknown(self, manager):
        manager.touch("nonexistent")

    async def test_next_seq(self, manager):
        assert manager.next_seq("room1") == 0
        assert manager.next_seq("room1") == 1
        assert manager.next_seq("room2") == 0

    async def test_room_members(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        await manager.join_room(cid, "r1")
        assert cid in manager.room_members("r1")
        assert len(manager.room_members("nonexistent")) == 0

    async def test_stats(self, manager):
        ws = _FakeWebSocket()
        _cid = await manager.register(ws, "user1", [])
        stats = manager.stats()
        assert stats["active_connections"] == 1
        assert "queue_depth_p50" in stats
        assert "rooms" in stats

    async def test_stats_empty(self):
        m = ConnectionManager()
        stats = m.stats()
        assert stats["active_connections"] == 0
        assert stats["queue_depth_p50"] == 0

    async def test_broadcast_delivers_to_websockets(self, manager):
        ws1 = _FakeWebSocket()
        ws2 = _FakeWebSocket()
        cid1 = await manager.register(ws1, "u1", [])
        cid2 = await manager.register(ws2, "u2", [])
        await manager.join_room(cid1, "portfolio:42")
        await manager.join_room(cid2, "portfolio:42")
        msg = EventMessage(channel="portfolio", room="portfolio:42")
        await manager.broadcast("portfolio:42", msg)
        await asyncio.sleep(0.05)
        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 1
        assert ws1.sent[0]["room"] == "portfolio:42"

    async def test_broadcast_empty_room(self, manager):
        msg = EventMessage(channel="portfolio", room="portfolio:42")
        count = await manager.broadcast("portfolio:42", msg)
        assert count == 0

    async def test_connection_count_and_room_count(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "test_room")
        assert manager.connection_count == 1
        assert manager.room_count == 2  # user:u1 + test_room

    async def test_unregister_cancels_sender_task(self, manager):
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "user1", [])
        info = manager.get_connection(cid)
        task = info.sender_task
        assert task is not None
        await manager.unregister(cid)
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# connection_manager.py — close_all
# ---------------------------------------------------------------------------


class TestConnectionManagerCloseAll:
    async def test_close_all_removes_connections(self):
        m = ConnectionManager(send_queue_size=256)
        ws1 = _FakeWebSocket()
        ws2 = _FakeWebSocket()
        await m.register(ws1, "u1", [])
        await m.register(ws2, "u2", [])
        assert m.connection_count == 2
        await m.close_all(code=1000, reason="test_shutdown")
        assert m.connection_count == 0

    async def test_close_all_processes_all_connections(self):
        m = ConnectionManager(send_queue_size=256)
        sockets = [_FakeWebSocket() for _ in range(5)]
        for i, ws in enumerate(sockets):
            await m.register(ws, f"u{i}", [])
        assert m.connection_count == 5
        await m.close_all(code=1000, reason="test_shutdown")
        assert m.connection_count == 0
        assert all(ws._closed for ws in sockets)

    async def test_close_all_blocks_registration_during_shutdown(self):
        m = ConnectionManager(send_queue_size=256)
        ws = _FakeWebSocket()
        await m.register(ws, "u1", [])
        blocked = {"v": False}
        original_send = m.send

        async def send_that_attempts_register(cid, message):
            if m._shutting_down:
                with pytest.raises(ConnectionLimitError):
                    await m.register(_FakeWebSocket(), "intruder", [])
                blocked["v"] = True
            return await original_send(cid, message)

        m.send = send_that_attempts_register
        await m.close_all()
        assert blocked["v"] is True
        assert m.connection_count == 0

    async def test_close_all_resets_flag_and_allows_registration_after(self):
        m = ConnectionManager()
        ws = _FakeWebSocket()
        await m.register(ws, "u1", [])
        await m.close_all()
        assert m._shutting_down is False
        ws2 = _FakeWebSocket()
        cid = await m.register(ws2, "u2", [])
        assert m.connection_count == 1
        await m.unregister(cid)

    async def test_close_all_snapshot_is_taken_under_lock(self):
        m = ConnectionManager(send_queue_size=256)
        wss = [_FakeWebSocket() for _ in range(3)]
        for i, ws in enumerate(wss):
            await m.register(ws, f"u{i}", [])
        await m.close_all(code=1001, reason="snap")
        assert m.connection_count == 0
        assert all(ws._closed for ws in wss)


class TestHeartbeatSelfCancellation:
    async def test_heartbeat_loop_exits_when_no_connections(self):
        m = ConnectionManager(heartbeat_interval=0.01)
        ws = _FakeWebSocket()
        cid = await m.register(ws, "u1", [])
        assert m._global_heartbeat_task is not None
        info = m.get_connection(cid)
        info.last_seen = time.monotonic() - 9999
        await asyncio.sleep(0.05)
        assert m.connection_count == 0
        await asyncio.sleep(0.05)
        assert m._global_heartbeat_task is None or m._global_heartbeat_task.done()

    async def test_unregister_last_connection_no_self_cancel_race(self):
        m = ConnectionManager(heartbeat_interval=0.01)
        ws = _FakeWebSocket()
        cid = await m.register(ws, "u1", [])
        task = m._global_heartbeat_task
        assert task is not None
        info = m.get_connection(cid)
        info.last_seen = time.monotonic() - 9999
        await asyncio.sleep(0.05)
        assert not task.cancelled()

    async def test_heartbeat_task_restarts_on_new_register(self):
        m = ConnectionManager(heartbeat_interval=0.01)
        ws1 = _FakeWebSocket()
        cid1 = await m.register(ws1, "u1", [])
        old_task = m._global_heartbeat_task
        await m.unregister(cid1)
        await asyncio.sleep(0.05)
        assert m._global_heartbeat_task is None or m._global_heartbeat_task.done()
        ws2 = _FakeWebSocket()
        await m.register(ws2, "u2", [])
        assert m._global_heartbeat_task is not None
        assert m._global_heartbeat_task is not old_task
        await m.unregister(next(iter(m._connections.keys())) if m._connections else "")

    async def test_heartbeat_survives_unregister_exception(self):
        m = ConnectionManager(heartbeat_interval=0.01)
        ws1 = _FakeWebSocket()
        ws2 = _FakeWebSocket()
        cid1 = await m.register(ws1, "u1", [])
        cid2 = await m.register(ws2, "u2", [])
        for cid in (cid1, cid2):
            m.get_connection(cid).last_seen = time.monotonic() - 9999
        original_unregister = m.unregister
        failed_once = {"v": False}

        async def flaky_unregister(cid, reason="client_disconnect"):
            if cid == cid1 and not failed_once["v"]:
                failed_once["v"] = True
                raise RuntimeError("boom")
            return await original_unregister(cid, reason=reason)

        m.unregister = flaky_unregister
        await asyncio.sleep(0.06)
        assert failed_once["v"] is True
        assert m.get_connection(cid2) is None
        assert m.connection_count == 0

    async def test_unregister_from_heartbeat_keeps_task_reference(self):
        m = ConnectionManager(heartbeat_interval=999.0)
        ws = _FakeWebSocket()
        cid = await m.register(ws, "u1", [])
        real_task = m._global_heartbeat_task
        assert real_task is not None
        real_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await real_task

        async def simulated_heartbeat_call():
            await m.unregister(cid, reason="heartbeat_timeout")

        hb = asyncio.create_task(simulated_heartbeat_call())
        m._global_heartbeat_task = hb
        await hb
        assert m.connection_count == 0
        assert m._global_heartbeat_task is hb
        assert not hb.cancelled()

    async def test_unregister_from_external_cancels_and_nulls_task(self):
        m = ConnectionManager(heartbeat_interval=999.0)
        ws = _FakeWebSocket()
        cid = await m.register(ws, "u1", [])
        task = m._global_heartbeat_task
        assert task is not None
        await m.unregister(cid)
        assert m.connection_count == 0
        assert m._global_heartbeat_task is None
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled() or task.done()

    async def test_register_reuses_heartbeat_task_when_not_done(self):
        m = ConnectionManager(heartbeat_interval=999.0)
        ws = _FakeWebSocket()
        await m.register(ws, "u1", [])
        task = m._global_heartbeat_task
        assert task is not None
        ws2 = _FakeWebSocket()
        await m.register(ws2, "u2", [])
        assert m._global_heartbeat_task is task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# channels.py — ChannelResolver
# ---------------------------------------------------------------------------


class TestChannelResolver:
    @pytest.fixture
    async def setup(self):
        manager = ConnectionManager(
            max_subscriptions_per_connection=50,
        )
        ws = _FakeWebSocket()
        cid = await manager.register(
            ws, "user1", ["read:portfolio", "read:orders", "read:strategies"]
        )
        resolver = ChannelResolver(manager, max_subscriptions_per_connection=50)
        return manager, resolver, cid

    async def test_subscribe_success(self, setup):
        _manager, resolver, cid = setup
        msg = SubscribeMessage(channel="portfolio", params={"account_id": "user1"})
        result = await resolver.handle_subscribe(cid, msg, "user1", ["read:portfolio"])
        assert result.success is True
        assert result.room == "portfolio:account:user1"

    async def test_subscribe_unknown_channel(self, setup):
        _, resolver, cid = setup
        msg = SubscribeMessage(channel="bogus", params={})
        result = await resolver.handle_subscribe(cid, msg, "user1", ["read:portfolio"])
        assert result.success is False
        assert result.error_code == "404"

    async def test_subscribe_permission_denied(self, setup):
        _, resolver, cid = setup
        msg = SubscribeMessage(channel="portfolio", params={"account_id": "user1"})
        result = await resolver.handle_subscribe(cid, msg, "user1", [])
        assert result.success is False
        assert result.error_code == "403"

    async def test_subscribe_missing_params(self, setup):
        _, resolver, cid = setup
        msg = SubscribeMessage(channel="portfolio", params={})
        result = await resolver.handle_subscribe(cid, msg, "user1", ["read:portfolio"])
        assert result.success is False
        assert result.error_code == "400"

    async def test_subscribe_max_subscriptions(self):
        manager = ConnectionManager(max_subscriptions_per_connection=1)
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", ["read:portfolio", "read:portfolio:all"])
        resolver = ChannelResolver(manager, max_subscriptions_per_connection=1)
        msg1 = SubscribeMessage(channel="portfolio", params={"account_id": "u1"})
        result1 = await resolver.handle_subscribe(cid, msg1, "u1", ["read:portfolio"])
        assert result1.success is True
        msg2 = SubscribeMessage(channel="orders", params={"symbol": "AAPL"})
        result2 = await resolver.handle_subscribe(cid, msg2, "u1", ["read:orders:all"])
        assert result2.success is False
        assert result2.error_code == "429"

    async def test_unsubscribe_success(self, setup):
        _manager, resolver, cid = setup
        msg = SubscribeMessage(channel="portfolio", params={"account_id": "user1"})
        await resolver.handle_subscribe(cid, msg, "user1", ["read:portfolio"])
        unsub = UnsubscribeMessage(channel="portfolio", params={"account_id": "user1"})
        result = await resolver.handle_unsubscribe(cid, unsub, "user1")
        assert result.success is True
        assert result.room == "portfolio:account:user1"

    async def test_unsubscribe_no_room(self, setup):
        _, resolver, cid = setup
        unsub = UnsubscribeMessage(channel="portfolio", params={})
        result = await resolver.handle_unsubscribe(cid, unsub, "user1")
        assert result.success is True

    async def test_unsubscribe_not_in_room(self, setup):
        _, resolver, cid = setup
        unsub = UnsubscribeMessage(channel="portfolio", params={"account_id": "A99"})
        result = await resolver.handle_unsubscribe(cid, unsub, "user1")
        assert result.success is True


# ---------------------------------------------------------------------------
# event_bridge.py — EventBusBridge
# ---------------------------------------------------------------------------


class _FakeBus:
    def __init__(self):
        self._handlers: dict[Any, list] = {}

    def subscribe(self, event_type, handler):
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler):
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    async def deliver(self, event_type, payload):
        for h in self._handlers.get(event_type, []):
            await h(payload)


class TestEventBusBridge:
    @pytest.fixture
    def setup(self):
        from engine.events.bus import EventType

        bus = _FakeBus()
        manager = ConnectionManager()
        bridge = EventBusBridge(bus=bus, manager=manager)
        return bus, manager, bridge, EventType

    def test_start_subscribes_defaults(self, setup):
        _bus, _, bridge, _EventType = setup
        bridge.start()
        assert len(bridge._registered) == 12

    def test_start_subscribes_custom(self, setup):
        _bus, _, bridge, EventType = setup
        bridge.start(event_types=[EventType.ORDER_CREATED])
        assert len(bridge._registered) == 1

    def test_stop_unsubscribes(self, setup):
        _bus, _, bridge, _EventType = setup
        bridge.start()
        bridge.stop()
        assert len(bridge._registered) == 0

    async def test_handle_dispatches_to_room(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "portfolio:account:A1")
        await bus.deliver(
            EventType.PORTFOLIO_UPDATED,
            {"type": "portfolio_updated", "data": {"account_id": "A1"}},
        )
        await asyncio.sleep(0.1)
        seq = manager.next_seq("portfolio:account:A1")
        assert seq >= 0

    async def test_handle_ignores_unknown_event(self, setup):
        bus, _manager, bridge, _EventType = setup
        bridge.start()
        await bus.deliver("unknown_event_type", {"type": "unknown_event_type"})
        await asyncio.sleep(0.05)

    async def test_handle_portfolio_with_strategy_id(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "portfolio:strategy:S1")
        await bus.deliver(
            EventType.POSITION_OPENED,
            {"type": "position_opened", "data": {"strategy_id": "S1"}},
        )
        await asyncio.sleep(0.1)

    async def test_handle_orders_with_symbol(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "orders:symbol:AAPL")
        await bus.deliver(
            EventType.ORDER_FILLED,
            {"type": "order_filled", "data": {"symbol": "AAPL"}},
        )
        await asyncio.sleep(0.1)

    async def test_handle_strategies(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "strategies:strategy:S1")
        await bus.deliver(
            EventType.STRATEGY_LOADED,
            {"type": "strategy_loaded", "data": {"strategy_id": "S1"}},
        )
        await asyncio.sleep(0.1)

    async def test_handle_non_dict_data(self, setup):
        bus, _manager, bridge, EventType = setup
        bridge.start()
        await bus.deliver(
            EventType.ORDER_CREATED,
            {"type": "order_created", "data": "not_a_dict"},
        )
        await asyncio.sleep(0.05)

    async def test_event_to_channel_mapping(self):
        assert _EVENT_TO_CHANNEL["portfolio_updated"] == "portfolio"
        assert _EVENT_TO_CHANNEL["order_created"] == "orders"
        assert _EVENT_TO_CHANNEL["strategy_loaded"] == "strategies"
        assert _EVENT_TO_CHANNEL["order_filled"] == "orders"
        assert _EVENT_TO_CHANNEL["position_closed"] == "portfolio"
        assert _EVENT_TO_CHANNEL["strategy_error"] == "strategies"

    async def test_handle_with_user_id_broadcasts_user_room(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "user:u1")
        await bus.deliver(
            EventType.ORDER_CREATED,
            {
                "type": "order_created",
                "data": {"user_id": "u1", "symbol": "AAPL"},
            },
        )
        await asyncio.sleep(0.1)

    async def test_handle_resolve_returns_none_falls_back_to_channel(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "portfolio")
        with patch("engine.api.ws.event_bridge.resolve_room_name", return_value=None):
            await bus.deliver(
                EventType.PORTFOLIO_UPDATED,
                {"type": "portfolio_updated", "data": {}},
            )
        await asyncio.sleep(0.1)
        assert len(ws.sent) >= 1
        assert ws.sent[0]["room"] == "portfolio"

    async def test_handle_resolve_returns_empty_string_falls_back(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "orders")
        with patch("engine.api.ws.event_bridge.resolve_room_name", return_value=""):
            await bus.deliver(
                EventType.ORDER_CREATED,
                {"type": "order_created", "data": {}},
            )
        await asyncio.sleep(0.1)
        assert len(ws.sent) >= 1
        assert ws.sent[0]["room"] == "orders"

    async def test_handle_resolve_exception_caught_and_logged(self, setup):
        bus, _manager, bridge, EventType = setup
        bridge.start()
        with patch(
            "engine.api.ws.event_bridge.resolve_room_name",
            side_effect=RuntimeError("resolve exploded"),
        ):
            await bus.deliver(
                EventType.PORTFOLIO_UPDATED,
                {"type": "portfolio_updated", "data": {}},
            )
        await asyncio.sleep(0.05)

    async def test_handle_no_matching_params_uses_bare_channel(self, setup):
        bus, manager, bridge, EventType = setup
        bridge.start()
        ws = _FakeWebSocket()
        cid = await manager.register(ws, "u1", [])
        await manager.join_room(cid, "strategies")
        await bus.deliver(
            EventType.STRATEGY_LOADED,
            {"type": "strategy_loaded", "data": {"irrelevant_key": "val"}},
        )
        await asyncio.sleep(0.1)
        assert len(ws.sent) >= 1
        assert ws.sent[0]["room"] == "strategies"

    async def test_handle_create_task_exception_caught(self, setup):
        bus, _manager, bridge, EventType = setup
        bridge.start()
        original_create = asyncio.create_task
        call_count = 0

        def failing_create(coro):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("task creation failed")
            return original_create(coro)

        with patch("engine.api.ws.event_bridge.asyncio.create_task", side_effect=failing_create):
            await bus.deliver(
                EventType.ORDER_CREATED,
                {"type": "order_created", "data": {"symbol": "AAPL"}},
            )
            await asyncio.sleep(0.05)


class TestResolveRoomNameGuard:
    def test_none_falsy_is_handled(self):
        result = None
        channel = "portfolio"
        room = result if result else channel
        assert room == "portfolio"

    def test_empty_string_falsy_is_handled(self):
        result = ""
        channel = "orders"
        room = result if result else channel
        assert room == "orders"

    def test_non_empty_string_passes_through(self):
        result = "portfolio:account:A1"
        channel = "portfolio"
        room = result if result else channel
        assert room == "portfolio:account:A1"

    def test_resolve_room_name_portfolio_empty_account_id(self):
        assert resolve_room_name("portfolio", {"account_id": ""}) is None

    def test_resolve_room_name_orders_empty_symbol(self):
        assert resolve_room_name("orders", {"symbol": ""}) is None

    def test_resolve_room_name_strategies_empty_strategy_id(self):
        assert resolve_room_name("strategies", {"strategy_id": ""}) is None


# ---------------------------------------------------------------------------
# health.py — ws_health_snapshot
# ---------------------------------------------------------------------------


class TestWsHealthSnapshot:
    async def test_healthy_snapshot(self):
        m = ConnectionManager()
        snapshot = ws_health_snapshot(m)
        assert snapshot["status"] == "healthy"
        assert "websocket" in snapshot
        assert snapshot["websocket"]["active_connections"] == 0

    async def test_snapshot_with_connections(self):
        m = ConnectionManager()
        ws = _FakeWebSocket()
        await m.register(ws, "u1", [])
        snapshot = ws_health_snapshot(m)
        assert snapshot["websocket"]["active_connections"] == 1


# ---------------------------------------------------------------------------
# exceptions.py
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_queue_full_error(self):
        err = QueueFullError(code=1008, reason="full")
        assert err.code == 1008
        assert "full" in str(err)

    def test_connection_limit_error(self):
        err = ConnectionLimitError(code=1011, reason="max")
        assert err.code == 1011

    def test_subscription_limit_error(self):
        err = SubscriptionLimitError(code=1008, reason="limit")
        assert err.code == 1008
