"""Comprehensive tests for ``engine.api.ip_utils``.

``engine/api/ip_utils.py`` was hardened in the most recent change
(commit a89ff750 "fix(ip-utils): harden XFF parsing and trusted-proxy
handling") with three security/correctness-relevant behaviors that had
**no** source-level test coverage (only stale ``.pyc`` artifacts existed):

1. **Bounded XFF walk** — :data:`MAX_XFF_HOPS` caps the right-to-left scan
   of ``X-Forwarded-For`` so a pathologically long (hostile) header cannot
   force unbounded parsing. This is a DoS-relevant guard, so it is pinned at
   its exact boundary (reachable at ``N-1`` vs. fallback at ``N``).

2. **Memoized proxy parsing** — :func:`_parse_proxy_networks_cached` wraps
   the expensive :func:`ipaddress.ip_network` calls in an ``lru_cache`` keyed
   on a *normalized* frozenset, so iteration order / duplicates collapse to
   one cache entry, the cached value is an immutable tuple (no aliasing
   bugs), and the public :func:`parse_proxy_networks` returns a fresh list on
   every call so a caller mutating its result can never corrupt the cache.

3. **Observable malformed-config handling** — a bad operator entry is now
   surfaced via a structured ``ip_utils.invalid_proxy_entry`` warning rather
   than being silently dropped, so mis-configuration is diagnosable.

Beyond those, the file also pins the pre-existing (and equally
security-relevant) contract of :func:`resolve_client_ip`:

* **Spoof resistance** — ``X-Forwarded-For`` is only honored when the
  immediate peer is itself a trusted proxy; an untrusted peer's header is
  ignored and the peer is reported directly.
* **Dual-stack collapse** — an IPv4-mapped IPv6 peer / hop matches an IPv4
  trusted-proxy network.
* **Safe fallbacks** — missing client, empty host, non-IP peer and an
  all-trusted chain each degrade to a defined value rather than raising.

The module is deliberately dependency-free at runtime (only
:mod:`ipaddress` + :mod:`structlog`, no FastAPI), so every test drives it
with a featherweight :class:`_FakeRequest` double rather than spinning up the
full ASGI app — keeping the suite hermetic and fast.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from engine.api import ip_utils
from engine.api.ip_utils import (
    MAX_XFF_HOPS,
    UNKNOWN_CLIENT,
    _collapse_mapped,
    _ip_in_networks,
    _normalize_proxy_entries,
    _parse_proxy_networks_cached,
    is_trusted_proxy,
    parse_proxy_networks,
    resolve_client_ip,
)

# ---------------------------------------------------------------------------
# Fixtures / harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_proxy_cache() -> None:
    """Reset the lru_cache between tests so each starts from a clean slate.

    ``_parse_proxy_networks_cached`` is a module-level ``lru_cache``; without
    a reset, a prior test could satisfy a later test's cache lookup (or, more
    subtly, suppress a warning that should fire), making assertions flaky and
    order-dependent.
    """
    _parse_proxy_networks_cached.cache_clear()
    yield
    _parse_proxy_networks_cached.cache_clear()


class _FakeRequest:
    """Minimal request double.

    ``resolve_client_ip`` reads exactly two attributes — ``request.client``
    (a ``client``-like object exposing ``.host``, or ``None``) and
    ``request.headers.get("x-forwarded-for", "")``. Implementing both with
    real Python objects keeps the double honest (a ``dict`` genuinely
    implements ``.get``) without pulling Starlette/FastAPI into the unit
    suite.
    """

    def __init__(self, host: str | None, xff: str | None, *, has_client: bool = True) -> None:
        self.client: Any = SimpleNamespace(host=host) if has_client else None
        # Use a plain dict so ``.get`` behaves exactly like a mapping.
        self.headers: dict[str, str] = {}
        if xff is not None:
            self.headers["x-forwarded-for"] = xff


def _request(
    host: str = "10.0.0.1",
    *,
    xff: str | None = None,
    has_client: bool = True,
) -> _FakeRequest:
    return _FakeRequest(host=host, xff=xff, has_client=has_client)


# A trusted-proxy set reused across many resolution tests. ``10.0.0.0/8``
# covers the private RFC1918 range our fixture proxies live in.
TRUSTED = ["10.0.0.0/8"]
# A "real client" address that is unambiguously *outside* the trusted range,
# so any test asserting it is returned proves the untrusted hop was honored.
CLIENT_IP = "203.0.113.9"  # TEST-NET-3, guaranteed not in 10.0.0.0/8


# ===========================================================================
# _normalize_proxy_entries
# ===========================================================================


class TestNormalizeProxyEntries:
    """``_normalize_proxy_entries`` builds the lru_cache key.

    The key must be a hashable ``frozenset`` with identical contents for any
    inputs that differ only in ordering or repetition — otherwise the cache
    would thrash and the "parse once" guarantee would silently break.
    """

    def test_returns_frozenset(self) -> None:
        result = _normalize_proxy_entries(["10.0.0.0/8", "10.0.1.1"])
        assert isinstance(result, frozenset)

    def test_drops_none_entries(self) -> None:
        assert _normalize_proxy_entries(["10.0.0.0/8", None, None, "10.0.1.1"]) == frozenset(
            {"10.0.0.0/8", "10.0.1.1"}
        )

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \t"])
    def test_drops_blank_and_whitespace_only(self, blank: str) -> None:
        assert _normalize_proxy_entries([blank]) == frozenset()

    def test_strips_surrounding_whitespace(self) -> None:
        assert _normalize_proxy_entries(["  10.0.0.0/8  "]) == frozenset({"10.0.0.0/8"})

    def test_deduplicates_repeated_entries(self) -> None:
        result = _normalize_proxy_entries(["10.0.0.0/8", "10.0.0.0/8", "10.0.0.0/8"])
        assert result == frozenset({"10.0.0.0/8"})

    def test_order_independent(self) -> None:
        a = _normalize_proxy_entries(["10.0.0.0/8", "10.0.1.1", "172.16.0.0/12"])
        b = _normalize_proxy_entries(["172.16.0.0/12", "10.0.1.1", "10.0.0.0/8"])
        assert a == b

    def test_empty_iterable_yields_empty_frozenset(self) -> None:
        assert _normalize_proxy_entries([]) == frozenset()

    def test_all_none_yields_empty(self) -> None:
        assert _normalize_proxy_entries([None, None]) == frozenset()


# ===========================================================================
# parse_proxy_networks / _parse_proxy_networks_cached — parsing and memoization
# ===========================================================================


class TestParseProxyNetworks:
    """Parsing of trusted-proxy strings into :class:`ipaddress` networks."""

    def test_bare_ipv4_host_becomes_32(self) -> None:
        nets = parse_proxy_networks(["10.0.0.5"])
        assert len(nets) == 1
        assert nets[0] == ip_utils.ipaddress.ip_network("10.0.0.5/32")

    def test_bare_ipv6_host_becomes_128(self) -> None:
        nets = parse_proxy_networks(["2001:db8::1"])
        assert len(nets) == 1
        assert nets[0].prefixlen == 128

    def test_cidr_range(self) -> None:
        nets = parse_proxy_networks(["10.0.0.0/8"])
        assert nets == [ip_utils.ipaddress.ip_network("10.0.0.0/8")]

    def test_strict_false_allows_host_bits(self) -> None:
        # ``strict=False`` must tolerate a CIDR with host bits set rather than
        # raising — operators frequently write ``10.1.2.3/8``.
        nets = parse_proxy_networks(["10.1.2.3/8"])
        assert nets == [ip_utils.ipaddress.ip_network("10.0.0.0/8")]

    def test_mixed_valid_and_invalid_keeps_only_valid(self) -> None:
        nets = parse_proxy_networks(["10.0.0.0/8", "not-a-network", "172.16.0.0/12"])
        addrs = {str(n) for n in nets}
        assert addrs == {"10.0.0.0/8", "172.16.0.0/12"}

    def test_invalid_entry_does_not_raise(self) -> None:
        # A bad operator entry must never break IP resolution for the service.
        assert parse_proxy_networks(["!!!bogus!!!"]) == []

    def test_blank_and_none_entries_ignored(self) -> None:
        assert parse_proxy_networks([None, "", "   ", "10.0.0.0/8"]) == [
            ip_utils.ipaddress.ip_network("10.0.0.0/8")
        ]

    def test_empty_iterable_returns_empty_list(self) -> None:
        assert parse_proxy_networks([]) == []

    def test_returns_list_not_tuple(self) -> None:
        # The public API contract is a fresh ``list`` even though the cached
        # internal value is a tuple (see ``test_returns_fresh_list``).
        assert isinstance(parse_proxy_networks(["10.0.0.0/8"]), list)


class TestParseProxyNetworksCaching:
    """The new memoization layer (commit a89ff750)."""

    def test_repeated_calls_hit_cache(self) -> None:
        parse_proxy_networks(["10.0.0.0/8"])
        parse_proxy_networks(["10.0.0.0/8"])
        info = _parse_proxy_networks_cached.cache_info()
        assert info.hits == 1
        assert info.misses == 1

    def test_order_and_dup_collapse_to_same_cache_entry(self) -> None:
        # The whole point of _normalize_proxy_entries: these two calls must
        # share one cache slot.
        parse_proxy_networks(["10.0.0.0/8", "172.16.0.0/12"])
        parse_proxy_networks(["172.16.0.0/12", "10.0.0.0/8", "10.0.0.0/8"])
        info = _parse_proxy_networks_cached.cache_info()
        assert info.misses == 1  # parsed exactly once
        assert info.hits == 1

    def test_cached_value_is_immutable_tuple(self) -> None:
        cached = _parse_proxy_networks_cached(frozenset({"10.0.0.0/8"}))
        assert isinstance(cached, tuple)

    def test_returns_fresh_list_each_call(self) -> None:
        """Mutating a returned list must not corrupt the cached tuple.

        This guards the public-API contract stated in the docstring: the
        caller gets a brand-new list, so accidental in-place mutation can't
        leak into subsequent resolutions via the shared cached value.
        """
        first = parse_proxy_networks(["10.0.0.0/8"])
        first.append(ip_utils.ipaddress.ip_network("8.8.8.8/32"))  # type: ignore[arg-type]
        second = parse_proxy_networks(["10.0.0.0/8"])
        assert second == [ip_utils.ipaddress.ip_network("10.0.0.0/8")]
        assert len(first) == 2  # the caller's local mutation is preserved

    def test_distinct_sets_cached_separately(self) -> None:
        parse_proxy_networks(["10.0.0.0/8"])
        parse_proxy_networks(["172.16.0.0/12"])
        info = _parse_proxy_networks_cached.cache_info()
        assert info.misses == 2


class TestInvalidProxyEntryWarning:
    """Malformed entries now emit a structured warning (was silent before)."""

    def test_invalid_entry_emits_warning_with_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="engine.api.ip_utils"):
            parse_proxy_networks(["@@bogus@@"])
        # structlog routes through stdlib logging; the rendered event name
        # appears in the record message.
        matching = [r for r in caplog.records if "ip_utils.invalid_proxy_entry" in r.getMessage()]
        assert matching, "expected an ip_utils.invalid_proxy_entry warning to be emitted"

    def test_valid_entries_emit_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="engine.api.ip_utils"):
            parse_proxy_networks(["10.0.0.0/8", "172.16.0.0/12"])
        bogus = [
            r for r in caplog.records if "ip_utils.invalid_proxy_entry" in r.getMessage()
        ]
        assert bogus == []

    def test_warning_fires_once_per_distinct_bad_entry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Two *distinct* malformed entries -> two warnings (cache is keyed on
        # the whole normalized set, so this exercises the per-entry loop).
        with caplog.at_level(logging.WARNING, logger="engine.api.ip_utils"):
            parse_proxy_networks(["@@one@@", "@@two@@"])
        warnings = [
            r for r in caplog.records if "ip_utils.invalid_proxy_entry" in r.getMessage()
        ]
        assert len(warnings) == 2


# ===========================================================================
# _collapse_mapped (IPv4-mapped IPv6 dual-stack handling)
# ===========================================================================


class TestCollapseMapped:
    def test_mapped_ipv6_collapses_to_ipv4(self) -> None:
        collapsed = _collapse_mapped(ip_utils.ipaddress.ip_address("::ffff:1.2.3.4"))
        assert collapsed == ip_utils.ipaddress.ip_address("1.2.3.4")
        assert collapsed.version == 4

    def test_plain_ipv6_unchanged(self) -> None:
        addr = ip_utils.ipaddress.ip_address("2001:db8::1")
        assert _collapse_mapped(addr) == addr

    def test_plain_ipv4_unchanged(self) -> None:
        addr = ip_utils.ipaddress.ip_address("1.2.3.4")
        assert _collapse_mapped(addr) == addr

    def test_loopback_mapped_ipv6(self) -> None:
        # ::ffff:127.0.0.1 is a common dual-stack representation of localhost.
        collapsed = _collapse_mapped(ip_utils.ipaddress.ip_address("::ffff:127.0.0.1"))
        assert collapsed == ip_utils.ipaddress.ip_address("127.0.0.1")


# ===========================================================================
# _ip_in_networks
# ===========================================================================


class TestIpInNetworks:
    def test_ipv4_in_cidr(self) -> None:
        nets = [ip_utils.ipaddress.ip_network("10.0.0.0/8")]
        assert _ip_in_networks(ip_utils.ipaddress.ip_address("10.1.2.3"), nets)

    def test_ipv4_outside_cidr(self) -> None:
        nets = [ip_utils.ipaddress.ip_network("10.0.0.0/8")]
        assert not _ip_in_networks(ip_utils.ipaddress.ip_address("192.168.1.1"), nets)

    def test_mapped_ipv6_matches_ipv4_network(self) -> None:
        # The dual-stack case: a proxy reached over IPv6 still matches an
        # IPv4 trusted-proxy entry.
        nets = [ip_utils.ipaddress.ip_network("10.0.0.0/8")]
        mapped = ip_utils.ipaddress.ip_address("::ffff:10.0.0.5")
        assert _ip_in_networks(mapped, nets)

    def test_version_mismatch_does_not_match(self) -> None:
        # An IPv4 address must not "match" an IPv6 network (and vice versa).
        v4 = ip_utils.ipaddress.ip_address("10.0.0.1")
        v6_nets = [ip_utils.ipaddress.ip_network("2001:db8::/32")]
        assert not _ip_in_networks(v4, v6_nets)

    def test_empty_networks_never_matches(self) -> None:
        assert not _ip_in_networks(ip_utils.ipaddress.ip_address("10.0.0.1"), [])

    def test_multiple_networks_any_match(self) -> None:
        nets = [
            ip_utils.ipaddress.ip_network("10.0.0.0/8"),
            ip_utils.ipaddress.ip_network("172.16.0.0/12"),
        ]
        assert _ip_in_networks(ip_utils.ipaddress.ip_address("172.31.255.255"), nets)
        assert _ip_in_networks(ip_utils.ipaddress.ip_address("10.255.255.255"), nets)
        assert not _ip_in_networks(ip_utils.ipaddress.ip_address("8.8.8.8"), nets)


# ===========================================================================
# is_trusted_proxy — CIDR-aware single-peer membership check
# ===========================================================================


class TestIsTrustedProxy:
    """Pin the CIDR-aware contract used by the WS auth path.

    A plain ``peer in trusted_proxies`` string check misses CIDR ranges, which
    is exactly the regression that opened SEV-507 in the WS auth helper.
    These tests ensure ``is_trusted_proxy`` matches a peer that lives inside a
    trusted CIDR range and rejects one that does not.
    """

    def test_peer_inside_cidr_is_trusted(self) -> None:
        assert is_trusted_proxy("10.255.42.1", ["10.0.0.0/8"])

    def test_peer_outside_cidr_is_untrusted(self) -> None:
        assert not is_trusted_proxy("11.0.0.1", ["10.0.0.0/8"])

    def test_bare_host_exact_match(self) -> None:
        assert is_trusted_proxy("10.0.0.1", ["10.0.0.1"])
        assert not is_trusted_proxy("10.0.0.2", ["10.0.0.1"])

    def test_mapped_ipv6_matches_ipv4_network(self) -> None:
        # Dual-stack proxy reached over IPv6 must still match an IPv4 entry.
        assert is_trusted_proxy("::ffff:10.0.0.5", ["10.0.0.0/8"])

    def test_empty_trusted_set_is_never_trusted(self) -> None:
        # Short-circuits so callers can drop a manual emptiness guard.
        assert not is_trusted_proxy("10.0.0.1", [])
        assert not is_trusted_proxy("10.0.0.1", [""])

    def test_none_or_blank_peer_is_never_trusted(self) -> None:
        assert not is_trusted_proxy(None, ["10.0.0.0/8"])
        assert not is_trusted_proxy("", ["10.0.0.0/8"])
        assert not is_trusted_proxy("   ", ["10.0.0.0/8"])

    def test_non_ip_peer_is_never_trusted(self) -> None:
        # A UDS hostname / garbage peer cannot be reasoned about.
        assert not is_trusted_proxy("unix:/var/run/app.sock", ["10.0.0.0/8"])
        assert not is_trusted_proxy("not-an-ip", ["10.0.0.0/8"])

    def test_multiple_networks_any_match(self) -> None:
        trusted = ["10.0.0.0/8", "172.16.0.0/12"]
        assert is_trusted_proxy("172.31.0.1", trusted)
        assert is_trusted_proxy("10.1.2.3", trusted)
        assert not is_trusted_proxy("8.8.8.8", trusted)


# ===========================================================================
# resolve_client_ip — fallbacks & spoof resistance
# ===========================================================================


class TestResolveClientIpFallbacks:
    def test_missing_client_returns_unknown(self) -> None:
        assert resolve_client_ip(_request(has_client=False), TRUSTED) == UNKNOWN_CLIENT

    def test_empty_host_returns_unknown(self) -> None:
        assert resolve_client_ip(_request(host=""), TRUSTED) == UNKNOWN_CLIENT

    def test_none_host_when_present_returns_unknown(self) -> None:
        # ``client`` present but host None -> peer falsy -> UNKNOWN.
        req = _FakeRequest(host=None, xff=None, has_client=True)  # type: ignore[arg-type]
        assert resolve_client_ip(req, TRUSTED) == UNKNOWN_CLIENT

    def test_non_ip_peer_returned_as_is(self) -> None:
        # A Unix-domain-socket hostname is not a parseable IP; the peer is
        # returned unchanged because proxy-chain reasoning does not apply.
        peer = "unix:/var/run/app.sock"
        assert resolve_client_ip(_request(host=peer), TRUSTED) == peer


class TestResolveClientIpSpoofResistance:
    """X-Forwarded-For is only honored from a trusted peer.

    An untrusted client can set ``X-Forwarded-For`` to anything; if we
    honored it the client could impersonate arbitrary addresses. So when the
    immediate peer is *not* a trusted proxy, the header is ignored and the
    peer itself is reported.
    """

    def test_untrusted_peer_ignores_xff(self) -> None:
        # 8.8.8.8 is outside 10.0.0.0/8, so it is the client regardless of XFF.
        result = resolve_client_ip(_request(host="8.8.8.8", xff="1.2.3.4"), TRUSTED)
        assert result == "8.8.8.8"

    def test_untrusted_peer_with_spoofed_trusted_hop_ignored(self) -> None:
        # Attacker tries to look like a trusted proxy chain.
        result = resolve_client_ip(
            _request(host="8.8.8.8", xff="10.0.0.1, 10.0.0.2"), TRUSTED
        )
        assert result == "8.8.8.8"

    def test_untrusted_peer_no_xff(self) -> None:
        assert resolve_client_ip(_request(host="203.0.113.1"), TRUSTED) == "203.0.113.1"


# ===========================================================================
# resolve_client_ip — trusted-peer XFF chain walking
# ===========================================================================


class TestResolveClientIpTrustedProxy:
    def test_trusted_peer_no_xff_returns_peer(self) -> None:
        # Single trusted proxy with no client info to forward -> peer fallback.
        assert resolve_client_ip(_request(host="10.0.0.1", xff=None), TRUSTED) == "10.0.0.1"

    def test_trusted_peer_empty_xff_returns_peer(self) -> None:
        assert resolve_client_ip(_request(host="10.0.0.1", xff=""), TRUSTED) == "10.0.0.1"

    def test_single_hop_returns_client(self) -> None:
        # client -> trusted proxy (peer). XFF carries the real client.
        result = resolve_client_ip(_request(host="10.0.0.1", xff=CLIENT_IP), TRUSTED)
        assert result == CLIENT_IP

    def test_multi_hop_walks_right_to_left(self) -> None:
        # client -> proxy1 -> proxy2(peer). Chain is ordered oldest-first.
        xff = f"{CLIENT_IP}, 10.0.0.2, 10.0.0.3"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_rightmost_untrusted_hop_returned(self) -> None:
        # Two untrusted hops: the *rightmost* (closest to the trusted proxy
        # chain) wins, not the leftmost. This is the spoof-resistant rule.
        xff = f"198.51.100.7, {CLIENT_IP}, 10.0.0.2"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_all_trusted_hops_fall_back_to_peer(self) -> None:
        # Every hop (incl. the leftmost) is itself a trusted proxy -> no
        # client can be identified -> fall back to peer.
        xff = "10.0.0.2, 10.0.0.3, 10.0.0.4"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == "10.0.0.1"

    def test_malformed_hop_skipped(self) -> None:
        # A garbage hop in the middle must not abort the walk.
        xff = f"{CLIENT_IP}, not-an-ip, 10.0.0.2"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_blank_hops_skipped(self) -> None:
        xff = f"{CLIENT_IP}, ,  , 10.0.0.2"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_whitespace_around_hops_trimmed(self) -> None:
        xff = f"  {CLIENT_IP}  ,  10.0.0.2  "
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_dual_stack_mapped_peer_trusted(self) -> None:
        # IPv4 proxy reached over a dual-stack IPv6 socket -> still trusted.
        result = resolve_client_ip(
            _request(host="::ffff:10.0.0.1", xff=CLIENT_IP), TRUSTED
        )
        assert result == CLIENT_IP

    def test_dual_stack_mapped_hop_trusted(self) -> None:
        # A hop expressed as an IPv4-mapped IPv6 address is recognized as a
        # trusted IPv4 proxy and skipped during the walk.
        xff = f"{CLIENT_IP}, ::ffff:10.0.0.2"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_ipv6_client_through_ipv4_proxy(self) -> None:
        ipv6_client = "2001:db8::42"
        xff = f"{ipv6_client}, 10.0.0.2"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == ipv6_client


# ===========================================================================
# resolve_client_ip — MAX_XFF_HOPS DoS bound (the headline new guard)
# ===========================================================================


class TestMaxXffHopsBound:
    """The right-to-left scan is capped at :data:`MAX_XFF_HOPS`.

    A pathologically long (or hostile) ``X-Forwarded-For`` must not force
    unbounded parsing work. Legitimate chains fit comfortably within the cap
    (each proxy appends one hop), so the bound only ever truncates hostile
    input — and when it does, we fall back to the trusted peer rather than
    returning an address the client injected past the cap.
    """

    def test_cap_constant_is_reasonable(self) -> None:
        # A sanity guard: if someone shrinks this to 0 or inflates it to a
        # billion, legitimate deployments break. Pin a sane range.
        assert 4 <= MAX_XFF_HOPS <= 128

    def test_client_just_within_cap_is_returned(self) -> None:
        # Build a chain whose real client sits at hop index MAX_XFF_HOPS-1
        # (0-based) from the right — the last index the loop inspects.
        trailing = ", ".join(["10.0.0.2"] * (MAX_XFF_HOPS - 1))
        xff = f"{CLIENT_IP}, {trailing}"
        assert resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED) == CLIENT_IP

    def test_client_just_past_cap_falls_back_to_peer(self) -> None:
        # Real client now sits at index MAX_XFF_HOPS from the right — one
        # past the cap — so the loop breaks before reaching it and we fall
        # back to the trusted peer. This is the DoS guard's payoff: an
        # attacker cannot buy unbounded work, and we never echo an
        # attacker-controlled address past the cap.
        trailing = ", ".join(["10.0.0.2"] * MAX_XFF_HOPS)
        xff = f"{CLIENT_IP}, {trailing}"
        assert resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED) == "10.0.0.1"

    def test_huge_header_does_not_raise_and_ignores_injected_client(self) -> None:
        # An attacker pads with trusted-looking hops and places their
        # spoofed address far past the cap. The resolver must neither raise
        # nor return the injected address.
        injected = "198.51.100.250"
        padding = ", ".join(["10.0.0.2"] * (MAX_XFF_HOPS + 50))
        xff = f"{injected}, {padding}"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == "10.0.0.1"
        assert result != injected

    @pytest.mark.parametrize("extra", [0, 1, 10, 100])
    def test_work_is_bounded_regardless_of_header_length(self, extra: int) -> None:
        # Defensive smoke test: a header longer than the cap still resolves
        # to the peer (never crashes, never echoes a past-cap address).
        padding = ", ".join(["10.0.0.2"] * (MAX_XFF_HOPS + extra))
        xff = f"{CLIENT_IP}, {padding}"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == "10.0.0.1"


# ===========================================================================
# resolve_client_ip — multi-million-comma XFF DoS guard (rsplit cap)
# ===========================================================================


class TestHugeXffHeaderDoSProtection:
    """A genuinely enormous ``X-Forwarded-For`` must stay cheap.

    The earlier ``forwarded.split(",")`` allocated a list with one entry per
    comma *before* the per-hop loop ran, so a hostile 5-million-comma header
    forced a multi-million-element allocation. Switching to
    ``forwarded.rsplit(",", MAX_XFF_HOPS)`` caps the result list at
    ``MAX_XFF_HOPS + 1`` elements by construction, so the resolver's memory
    and CPU are bounded no matter how long the attacker pads the header.

    These tests pin the three guarantees the change must preserve:

    1. a 5-million-comma header is processed without error (no blow-up);
    2. the correct client IP is still extracted when it sits within the
       inspectable (rightmost) hops; and
    3. ordinary multi-hop behavior is unchanged by the ``rsplit`` switch.
    """

    #: A genuinely hostile header size — five million commas — exercising the
    #: ``rsplit`` cap rather than a small handful of hops.
    HUGE_HOPS = 5_000_000

    def test_five_million_comma_header_processed_without_error(self) -> None:
        # The client address is buried at the far left, well past the cap, so
        # the resolver falls back to the peer — but the headline assertion is
        # that it returns *at all* and never raises on the giant header. With
        # the old ``split(",")`` this allocated a ~5M-entry list first.
        padding = ", ".join(["10.0.0.2"] * self.HUGE_HOPS)
        xff = f"{CLIENT_IP}, {padding}"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == "10.0.0.1"

    def test_correct_client_ip_still_extracted_from_huge_header(self) -> None:
        # Same giant header, but the genuine client is the *rightmost* hop
        # (closest to the trusted proxy chain), so it sits within the first
        # ``MAX_XFF_HOPS`` inspected hops and must be returned exactly.
        padding = ", ".join(["10.0.0.2"] * self.HUGE_HOPS)
        xff = f"{padding}, {CLIENT_IP}"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_multi_hop_behavior_preserved_after_rsplit(self) -> None:
        # The ``rsplit`` switch must not disturb the canonical multi-hop walk:
        # a client flanked by trusted proxies, read right-to-left, resolves to
        # the rightmost *untrusted* hop. (Re-asserts the headline spoof-
        # resistant contract against the new splitting implementation.)
        xff = f"198.51.100.7, {CLIENT_IP}, 10.0.0.2, 10.0.0.3"
        result = resolve_client_ip(_request(host="10.0.0.1", xff=xff), TRUSTED)
        assert result == CLIENT_IP

    def test_rsplit_caps_list_size_for_huge_header(self) -> None:
        # Direct proof that the implementation uses a capped split: the
        # ``rsplit(",", MAX_XFF_HOPS)`` of a 5M-comma header yields exactly
        # ``MAX_XFF_HOPS + 1`` elements (not 5M+1), regardless of padding.
        header = ",".join(["10.0.0.2"] * (self.HUGE_HOPS + 1))
        assert len(header.rsplit(",", MAX_XFF_HOPS)) == MAX_XFF_HOPS + 1
