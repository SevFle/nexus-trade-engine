"""Unit tests for :mod:`engine.api.ip_utils`.

Covers the three hardening behaviours required of the module:

* the ``MAX_XFF_HOPS`` cap truncating over-long ``X-Forwarded-For`` chains,
* a structured warning emitted for each malformed trusted-proxy entry, and
* memoization of proxy-network parsing across calls (``lru_cache`` reuse).

Plus regression coverage for the core spoof-resistant resolution logic.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from engine.api.ip_utils import (
    MAX_XFF_HOPS,
    UNKNOWN_CLIENT,
    _parse_proxy_networks_cached,
    parse_proxy_networks,
    resolve_client_ip,
)

TRUSTED = "10.0.0.1"
CLIENT = "203.0.113.99"


# --------------------------------------------------------------------------- #
# Lightweight request doubles (the module is FastAPI-free at runtime).
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in for ``starlette.requests.Request``."""

    def __init__(self, peer: str | None, xff: str | None = None) -> None:
        self.client: Any = _FakeClient(peer) if peer is not None else None
        # starlette headers are case-insensitive; we only ever read the
        # lowercased key, so a plain dict suffices here.
        self.headers: dict[str, str] = {}
        if xff is not None:
            self.headers["x-forwarded-for"] = xff


def _xff_chain(client: str, trusted: str, n_trusted: int) -> str:
    """Build an XFF header ``client, trusted, trusted, ...`` (left = client)."""
    return ", ".join([client] + [trusted] * n_trusted)


@pytest.fixture(autouse=True)
def _clear_proxy_cache() -> None:
    """Reset the ``lru_cache`` between tests so they are order-independent."""
    _parse_proxy_networks_cached.cache_clear()
    yield
    _parse_proxy_networks_cached.cache_clear()


# --------------------------------------------------------------------------- #
# Constants & sanity
# --------------------------------------------------------------------------- #
class TestConstants:
    def test_max_xff_hops_is_16(self) -> None:
        assert MAX_XFF_HOPS == 16

    def test_unknown_client_sentinel(self) -> None:
        assert UNKNOWN_CLIENT == "unknown"


# --------------------------------------------------------------------------- #
# MAX_XFF_HOPS truncation
# --------------------------------------------------------------------------- #
class TestXffHopCap:
    @pytest.mark.parametrize(
        ("n_trusted", "expected"),
        [
            # 16 hops total (1 client + 15 trusted): within the cap, the client
            # at the far left is reached on the 16th examined hop.
            (15, CLIENT),
            # 17 hops total (1 client + 16 trusted): the cap is hit after 16
            # trusted hops, so the client is never reached -> fall back to peer.
            (16, TRUSTED),
            # Well within the cap: client is reached.
            (1, CLIENT),
            (0, CLIENT),
        ],
    )
    def test_chain_length_boundary(self, n_trusted: int, expected: str) -> None:
        request = _FakeRequest(
            peer=TRUSTED,
            xff=_xff_chain(CLIENT, TRUSTED, n_trusted),
        )
        assert resolve_client_ip(request, [TRUSTED]) == expected

    def test_chain_longer_than_max_hops_is_truncated(self) -> None:
        """A chain of 30 hops truncates at MAX_XFF_HOPS and returns the peer.

        The genuine untrusted client sits at the far left; with 29 trusted
        hops to its right, the capped right-to-left walk only inspects the 16
        rightmost (all trusted) and never reaches the client.
        """
        request = _FakeRequest(
            peer=TRUSTED,
            xff=_xff_chain(CLIENT, TRUSTED, n_trusted=29),
        )
        assert resolve_client_ip(request, [TRUSTED]) == TRUSTED

    def test_client_within_cap_is_returned(self) -> None:
        """A long-ish chain that still fits inside the cap resolves the client."""
        request = _FakeRequest(
            peer=TRUSTED,
            xff=_xff_chain(CLIENT, TRUSTED, n_trusted=MAX_XFF_HOPS - 1),
        )
        assert resolve_client_ip(request, [TRUSTED]) == CLIENT

    def test_truncation_falls_back_to_peer_not_unknown(self) -> None:
        request = _FakeRequest(
            peer=TRUSTED,
            xff=_xff_chain(CLIENT, TRUSTED, n_trusted=20),
        )
        # Truncation must fall back to the trusted peer, never UNKNOWN_CLIENT.
        assert resolve_client_ip(request, [TRUSTED]) == TRUSTED
        assert resolve_client_ip(request, [TRUSTED]) != UNKNOWN_CLIENT

    def test_huge_chain_does_not_scan_every_hop(self) -> None:
        """A 10_000-hop header must return quickly (the cap makes it O(1)-ish)."""
        giant = ", ".join([CLIENT] + [TRUSTED] * 10_000)
        request = _FakeRequest(peer=TRUSTED, xff=giant)
        # All hops within the cap are trusted, so we fall back to the peer
        # without ever scanning the whole header.
        assert resolve_client_ip(request, [TRUSTED]) == TRUSTED


# --------------------------------------------------------------------------- #
# Malformed CIDR -> structured warning
# --------------------------------------------------------------------------- #
class TestMalformedProxyWarning:
    def test_malformed_entry_emits_structlog_warning(self) -> None:
        from structlog.testing import capture_logs

        with capture_logs() as cap_logs:
            networks = parse_proxy_networks(["10.0.0.0/8", "not-a-cidr", "1.2.3.4"])

        # Valid entries are still parsed. The cache key is normalized to a
        # sorted tuple (for order-independent lookups), so we compare as a
        # set rather than asserting insertion order.
        assert set(networks) == {
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("1.2.3.4"),
        }

        warnings = [e for e in cap_logs if e.get("log_level") == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["event"] == "ip_utils.invalid_proxy_entry"
        assert warnings[0]["entry"] == "not-a-cidr"
        # The underlying ValueError message is included for diagnostics.
        assert "error" in warnings[0]
        assert warnings[0]["error"]

    def test_each_malformed_entry_warns_independently(self) -> None:
        from structlog.testing import capture_logs

        with capture_logs() as cap_logs:
            parse_proxy_networks(["bad-1", "10.0.0.1", "bad-2", "bad-3"])

        warnings = [e for e in cap_logs if e.get("log_level") == "warning"]
        bad_entries = {w["entry"] for w in warnings}
        assert bad_entries == {"bad-1", "bad-2", "bad-3"}
        assert len(warnings) == 3

    def test_valid_only_emits_no_warning(self) -> None:
        from structlog.testing import capture_logs

        with capture_logs() as cap_logs:
            parse_proxy_networks(["10.0.0.0/8", "192.168.1.1", "172.16.0.0/12"])

        assert not [e for e in cap_logs if e.get("log_level") == "warning"]

    def test_blank_and_none_entries_are_silently_ignored(self) -> None:
        from structlog.testing import capture_logs

        with capture_logs() as cap_logs:
            networks = parse_proxy_networks(
                ["10.0.0.1", "", "   ", None]  # type: ignore[list-item]
            )

        assert networks == [ipaddress.ip_network("10.0.0.1")]
        assert not [e for e in cap_logs if e.get("log_level") == "warning"]

    def test_invalid_cidr_prefix_is_warned(self) -> None:
        """A syntactically-IP-shaped but invalid CIDR is flagged, not crashed."""
        from structlog.testing import capture_logs

        with capture_logs() as cap_logs:
            parse_proxy_networks(["10.0.0.0/99"])

        warnings = [e for e in cap_logs if e.get("log_level") == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["entry"] == "10.0.0.0/99"


# --------------------------------------------------------------------------- #
# lru_cache reuse
# --------------------------------------------------------------------------- #
class TestProxyNetworkCaching:
    def test_repeated_calls_hit_the_cache(self) -> None:
        proxies = ["10.0.0.1", "10.0.0.0/8", "192.168.1.1"]

        parse_proxy_networks(proxies)
        info_after_first = _parse_proxy_networks_cached.cache_info()
        assert info_after_first.misses == 1
        assert info_after_first.hits == 0

        parse_proxy_networks(proxies)
        info_after_second = _parse_proxy_networks_cached.cache_info()
        assert info_after_second.misses == 1  # no new parse
        assert info_after_second.hits == 1  # reused

    def test_cache_is_order_independent(self) -> None:
        """A set input with different iteration orders still shares one entry."""
        parse_proxy_networks({"10.0.0.1", "10.0.0.0/8"})
        parse_proxy_networks({"10.0.0.0/8", "10.0.0.1"})

        info = _parse_proxy_networks_cached.cache_info()
        assert info.misses == 1
        assert info.hits == 1

    def test_different_proxy_sets_miss(self) -> None:
        parse_proxy_networks(["10.0.0.1"])
        parse_proxy_networks(["10.0.0.2"])

        info = _parse_proxy_networks_cached.cache_info()
        assert info.misses == 2
        assert info.hits == 0

    def test_cached_result_is_not_mutated_across_calls(self) -> None:
        """The public API returns fresh lists so cached state stays intact."""
        first = parse_proxy_networks(["10.0.0.1"])
        first.append(ipaddress.ip_network("8.8.8.8"))  # mutate the caller's list

        second = parse_proxy_networks(["10.0.0.1"])
        assert second == [ipaddress.ip_network("10.0.0.1")]
        assert ipaddress.ip_network("8.8.8.8") not in second

    def test_resolve_client_ip_uses_cache_for_repeated_proxy_sets(self) -> None:
        request = _FakeRequest(peer=TRUSTED, xff=CLIENT)
        resolve_client_ip(request, [TRUSTED])
        resolve_client_ip(request, [TRUSTED])

        info = _parse_proxy_networks_cached.cache_info()
        assert info.misses == 1
        assert info.hits == 1

    def test_duplicate_entries_collapse_to_same_cache_key(self) -> None:
        parse_proxy_networks(["10.0.0.1", "10.0.0.1", "10.0.0.1"])
        parse_proxy_networks(["10.0.0.1"])

        info = _parse_proxy_networks_cached.cache_info()
        assert info.misses == 1
        assert info.hits == 1


# --------------------------------------------------------------------------- #
# Core resolution regression coverage
# --------------------------------------------------------------------------- #
class TestResolveClientIp:
    def test_untrusted_peer_returned_directly(self) -> None:
        # Peer is NOT a trusted proxy -> it is the client; XFF is ignored.
        request = _FakeRequest(peer="198.51.100.7", xff="9.9.9.9")
        assert resolve_client_ip(request, [TRUSTED]) == "198.51.100.7"

    def test_first_untrusted_hop_rightmost_is_returned(self) -> None:
        # XFF: client, trusted-proxy. Walking right-to-left skips the trusted
        # proxy and returns the genuine client.
        request = _FakeRequest(peer=TRUSTED, xff=f"{CLIENT}, {TRUSTED}")
        assert resolve_client_ip(request, [TRUSTED]) == CLIENT

    def test_no_xff_falls_back_to_peer(self) -> None:
        request = _FakeRequest(peer=TRUSTED, xff=None)
        assert resolve_client_ip(request, [TRUSTED]) == TRUSTED

    def test_all_trusted_chain_falls_back_to_peer(self) -> None:
        request = _FakeRequest(peer=TRUSTED, xff=f"{TRUSTED}, {TRUSTED}")
        assert resolve_client_ip(request, [TRUSTED]) == TRUSTED

    def test_cidr_range_trust(self) -> None:
        # Peer inside 10.0.0.0/8 is trusted; client is the untrusted hop.
        request = _FakeRequest(
            peer="10.5.5.5",
            xff=f"{CLIENT}, 10.5.5.5",
        )
        assert resolve_client_ip(request, ["10.0.0.0/8"]) == CLIENT

    def test_ipv4_mapped_ipv6_proxy_matches_ipv4_network(self) -> None:
        request = _FakeRequest(
            peer="::ffff:10.0.0.1",
            xff=f"{CLIENT}, ::ffff:10.0.0.1",
        )
        assert resolve_client_ip(request, ["10.0.0.0/8"]) == CLIENT

    def test_missing_client_returns_unknown(self) -> None:
        request = _FakeRequest(peer=None)
        assert resolve_client_ip(request, [TRUSTED]) == UNKNOWN_CLIENT

    def test_unparseable_peer_returned_as_is(self) -> None:
        request = _FakeRequest(peer="unix:/var/run/app.sock", xff=CLIENT)
        assert resolve_client_ip(request, [TRUSTED]) == "unix:/var/run/app.sock"

    def test_empty_xff_header_falls_back_to_peer(self) -> None:
        request = _FakeRequest(peer=TRUSTED, xff="")
        assert resolve_client_ip(request, [TRUSTED]) == TRUSTED

    def test_whitespace_only_hops_are_skipped(self) -> None:
        request = _FakeRequest(peer=TRUSTED, xff=f"  , {CLIENT},  ")
        assert resolve_client_ip(request, [TRUSTED]) == CLIENT

    def test_malformed_hop_is_skipped(self) -> None:
        # A garbage hop is skipped; the next untrusted hop wins.
        request = _FakeRequest(peer=TRUSTED, xff=f"{CLIENT}, not-an-ip, {TRUSTED}")
        assert resolve_client_ip(request, [TRUSTED]) == CLIENT


# --------------------------------------------------------------------------- #
# parse_proxy_networks behaviour
# --------------------------------------------------------------------------- #
class TestParseProxyNetworks:
    def test_bare_host_becomes_full_mask_network(self) -> None:
        (net,) = parse_proxy_networks(["10.0.0.1"])
        assert net == ipaddress.ip_network("10.0.0.1/32")

    def test_strict_false_tolerates_host_bits(self) -> None:
        (net,) = parse_proxy_networks(["10.0.0.5/8"])
        assert net == ipaddress.ip_network("10.0.0.0/8")

    def test_mixed_ipv4_ipv6(self) -> None:
        nets = parse_proxy_networks(["10.0.0.0/8", "2001:db8::/32"])
        assert ipaddress.ip_network("10.0.0.0/8") in nets
        assert ipaddress.ip_network("2001:db8::/32") in nets

    def test_dedupes_identical_entries(self) -> None:
        nets = parse_proxy_networks(["10.0.0.1", "10.0.0.1", "10.0.0.1"])
        assert nets == [ipaddress.ip_network("10.0.0.1")]
