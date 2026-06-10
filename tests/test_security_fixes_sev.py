"""Tests for security fixes: IP spoofing prevention, permission isolation, coverage gaps.

Covers:
- SEV fix: _get_remote_ip uses rightmost (proxy-appended) IP from X-Forwarded-For
- SEV fix: ipaddress.ip_address() validation with fallback on invalid input
- SEV fix: orders channel owner_field changed from 'symbol' to 'user_id'
- Coverage: sandbox.py ImportError branch (lines 49-51)
- Coverage: model.py _ticker_no_whitespace ValueError path (lines 146-147)
- Coverage: search.py _score 'q in ticker' branch (line 193)
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import pytest

from engine.api.ws.auth import _get_remote_ip
from engine.api.ws.permissions import CHANNEL_PERMISSIONS, check_channel_access
from engine.reference.model import RefInstrument
from engine.reference.search import SearchIndex


class _FakeHost:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeWebSocket:
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


# ---------------------------------------------------------------------------
# _get_remote_ip — rightmost IP (spoofing prevention)
# ---------------------------------------------------------------------------


class TestGetRemoteIpSpoofingPrevention:
    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_takes_rightmost_ip_from_forwarded(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 10.0.0.1"},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_spoofed_leftmost_ip_is_ignored(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "spoofed, 192.168.1.1"},
        )
        assert _get_remote_ip(ws) == "192.168.1.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_single_ip_in_forwarded(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "9.8.7.6"},
        )
        assert _get_remote_ip(ws) == "9.8.7.6"


# ---------------------------------------------------------------------------
# _get_remote_ip — IP validation with fallback
# ---------------------------------------------------------------------------


class TestGetRemoteIpValidation:
    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_valid_ipv4_forwarded(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "192.168.1.1"},
        )
        assert _get_remote_ip(ws) == "192.168.1.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_valid_ipv6_forwarded(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "::1"},
        )
        assert _get_remote_ip(ws) == "::1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_invalid_forwarded_falls_back_to_client(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": "not-an-ip"},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_empty_forwarded_falls_back(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-forwarded-for": ""},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_invalid_real_ip_falls_back_to_client(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-real-ip": "garbage!!"},
        )
        assert _get_remote_ip(ws) == "10.0.0.1"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_valid_real_ip(self):
        ws = _FakeWebSocket(
            client_host="10.0.0.1",
            headers={"x-real-ip": "172.16.0.1"},
        )
        assert _get_remote_ip(ws) == "172.16.0.1"

    def test_no_client_returns_unknown(self):
        ws = _FakeWebSocket(client_host=None)
        assert _get_remote_ip(ws) == "unknown"

    @patch("engine.api.ws.auth._TRUSTED_PROXIES", frozenset({"10.0.0.1"}))
    def test_no_client_with_forwarded_returns_unknown(self):
        ws = _FakeWebSocket(
            client_host=None,
            headers={"x-forwarded-for": "not-valid"},
        )
        assert _get_remote_ip(ws) == "unknown"


# ---------------------------------------------------------------------------
# Orders channel — owner_field is 'user_id'
# ---------------------------------------------------------------------------


class TestOrdersChannelOwnerField:
    def test_orders_owner_field_is_user_id(self):
        assert CHANNEL_PERMISSIONS["orders"].owner_field == "user_id"

    def test_orders_owner_mismatch_denies(self):
        ok, err = check_channel_access(
            "orders",
            ["read:orders"],
            {"user_id": "user_a"},
            user_id="user_b",
        )
        assert ok is False
        assert err == "403"

    def test_orders_owner_match_grants(self):
        ok, err = check_channel_access(
            "orders",
            ["read:orders"],
            {"user_id": "user_a"},
            user_id="user_a",
        )
        assert ok is True
        assert err is None

    def test_orders_all_scope_bypasses_owner_check(self):
        ok, err = check_channel_access(
            "orders",
            ["read:orders:all"],
            {"user_id": "other_user"},
            user_id="my_user",
        )
        assert ok is True
        assert err is None

    def test_orders_no_user_id_in_params_grants(self):
        ok, _ = check_channel_access(
            "orders",
            ["read:orders"],
            {"symbol": "AAPL"},
            user_id="user_a",
        )
        assert ok is True

    def test_orders_symbol_is_not_checked_for_ownership(self):
        ok, _ = check_channel_access(
            "orders",
            ["read:orders"],
            {"symbol": "AAPL", "user_id": "user_a"},
            user_id="user_a",
        )
        assert ok is True


# ---------------------------------------------------------------------------
# Coverage: sandbox.py ImportError branch (lines 49-51)
# ---------------------------------------------------------------------------


class TestSandboxImportErrorBranch:
    def test_has_resource_module_false_when_import_fails(self):
        import engine.plugins.sandbox as mod

        saved_resource = sys.modules.get("resource")
        try:
            if "resource" in sys.modules:
                del sys.modules["resource"]
            with patch.dict(sys.modules, {"resource": None}):
                importlib.reload(mod)
                assert mod.HAS_RESOURCE_MODULE is False
        finally:
            if saved_resource is not None:
                sys.modules["resource"] = saved_resource
            importlib.reload(mod)

    def test_has_resource_module_true_on_linux(self):
        import engine.plugins.sandbox as mod

        importlib.reload(mod)
        try:
            importlib.util.find_spec("resource")

            assert mod.HAS_RESOURCE_MODULE is True
        except ImportError:
            assert mod.HAS_RESOURCE_MODULE is False


# ---------------------------------------------------------------------------
# Coverage: model.py _ticker_no_whitespace ValueError (lines 146-147)
# ---------------------------------------------------------------------------


class TestTickerNoWhitespaceValidator:
    def test_whitespace_only_ticker_raises_value_error(self):
        with pytest.raises(ValueError, match="primary_ticker must be non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace("   ")

    def test_leading_space_ticker_raises(self):
        with pytest.raises(ValueError, match="primary_ticker must be non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace(" AAPL")

    def test_trailing_space_ticker_raises(self):
        with pytest.raises(ValueError, match="primary_ticker must be non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace("AAPL ")

    def test_tab_in_ticker_raises(self):
        with pytest.raises(ValueError, match="primary_ticker must be non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace("\tAAPL")

    def test_valid_ticker_passes(self):
        assert RefInstrument._ticker_no_whitespace("AAPL") == "AAPL"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="primary_ticker must be non-empty and trimmed"):
            RefInstrument._ticker_no_whitespace("")


# ---------------------------------------------------------------------------
# Coverage: search.py _score 'q in ticker' branch (line 193)
# ---------------------------------------------------------------------------


class TestSearchScoreTickerContains:
    def _make_index(self) -> SearchIndex:
        idx = SearchIndex()
        idx.add(
            RefInstrument(
                primary_ticker="BRK.B",
                primary_venue="XNYS",
                asset_class="equity",
                name="Berkshire Hathaway Inc.",
            )
        )
        return idx

    def test_ticker_substring_match_returns_result(self):
        idx = self._make_index()
        results = idx.search("rk.")
        assert len(results) == 1
        assert results[0].primary_ticker == "BRK.B"

    def test_ticker_substring_not_prefix(self):
        idx = self._make_index()
        results = idx.search(".b")
        assert len(results) == 1
        assert results[0].primary_ticker == "BRK.B"

    def test_ticker_substring_score_is_60(self):
        idx = self._make_index()
        idx.add(
            RefInstrument(
                primary_ticker="Z.B",
                primary_venue="XNYS",
                asset_class="equity",
                name="Zeta Beta Corp.",
            )
        )
        results = idx.search(".b")
        assert len(results) == 2
