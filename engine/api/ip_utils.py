"""Client IP resolution honoring trusted reverse proxies.

When the API is deployed behind a load balancer / reverse proxy, the raw
``request.client.host`` is the *proxy's* address, not the end user's. The
``X-Forwarded-For`` header carries the original client chain, but it must only
be trusted when the immediate peer is itself a *trusted* proxy — otherwise a
client can trivially spoof its address by setting the header.

:func:`resolve_client_ip` encapsulates that logic:

* Peer and trusted-proxy entries are parsed with :mod:`ipaddress`
  (:func:`ipaddress.ip_address` / :func:`ipaddress.ip_network`) so both single
  hosts and CIDR ranges are supported, and matching is done via proper network
  containment rather than fragile string comparison.
* IPv4-mapped IPv6 addresses (e.g. ``::ffff:1.2.3.4``) are collapsed to their
  IPv4 form before comparison, so a trusted IPv4 proxy reached over a dual-stack
  listener still matches.
* When the peer is trusted, the ``X-Forwarded-For`` chain is walked
  right-to-left and the first hop that is *not* a trusted proxy is reported as
  the real client. This is the standard, spoof-resistant interpretation: each
  trusted proxy appends the previous hop, so the rightmost untrusted entry is
  the genuine origin. The walk is bounded by :data:`MAX_XFF_HOPS` so a
  pathologically long header cannot force unbounded work.

* Trusted-proxy parsing is memoized (:func:`_parse_proxy_networks_cached`) so
  the :func:`ipaddress.ip_network` calls happen at most once per distinct
  proxy set, and each malformed entry is surfaced via a structured ``warning``
  log rather than being silently dropped.

This module only needs :mod:`ipaddress` and :mod:`structlog` (no FastAPI
imports at runtime) so it can be unit-tested in isolation with lightweight
request doubles.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.requests import Request

#: Sentinel returned when no client address can be determined at all
#: (e.g. the ASGI ``client`` tuple is missing).
UNKNOWN_CLIENT = "unknown"

#: Maximum number of ``X-Forwarded-For`` hops to inspect when walking the
#: chain right-to-left. Bounds the cost of processing a pathologically long
#: (or hostile) header so a single request cannot force unbounded parsing of
#: an attacker-controlled value. Each trusted proxy appends exactly one hop,
#: so any legitimate chain fits comfortably within this cap.
MAX_XFF_HOPS = 16

_log = structlog.get_logger(__name__)

_IPvXAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPvXNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _normalize_proxy_entries(
    trusted_proxies: Iterable[str],
) -> frozenset[str]:
    """Normalize proxy entries into a hashable, deduplicated key.

    ``None`` and blank/whitespace-only entries are dropped and the remaining
    entries are stripped and de-duplicated. The result is a ``frozenset`` so
    two calls whose inputs merely differ in iteration order or repetition
    collapse to the same cache key (see :func:`_parse_proxy_networks_cached`).
    """
    entries: set[str] = set()
    for raw in trusted_proxies:
        if raw is None:
            continue
        entry = raw.strip()
        if entry:
            entries.add(entry)
    return frozenset(entries)


@lru_cache(maxsize=256)
def _parse_proxy_networks_cached(
    entries: frozenset[str],
) -> tuple[_IPvXNetwork, ...]:
    """Parse a normalized set of proxy entries into IP networks (cached).

    The expensive :func:`ipaddress.ip_network` calls run at most once per
    distinct entry-set. Malformed entries are skipped after emitting a
    structured ``warning`` carrying the offending entry and the parser error,
    so a bad operator config is observable rather than silently ignored.

    An immutable ``tuple`` is returned (and cached) so callers can never
    mutate the shared cached value.
    """
    networks: list[_IPvXNetwork] = []
    for entry in entries:
        try:
            # ip_network accepts both bare hosts (-> /32 or /128) and CIDR
            # ranges. ``strict=False`` tolerates host bits set in a CIDR.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            _log.warning(
                "ip_utils.invalid_proxy_entry",
                entry=entry,
                error=str(exc),
            )
    return tuple(networks)


def parse_proxy_networks(
    trusted_proxies: Iterable[str],
) -> list[_IPvXNetwork]:
    """Parse an iterable of trusted-proxy entries into IP networks.

    Each entry may be a bare address (``"10.0.0.1"``) or a CIDR range
    (``"10.0.0.0/8"``). Blank/``None`` entries are ignored and unparseable
    values are skipped — after emitting a structured warning — rather than
    raising, so a malformed operator config cannot break IP resolution for the
    whole service. Parsing is memoized via :func:`_parse_proxy_networks_cached`
    so repeated calls with the same proxy set reuse the cached result.

    A fresh list is returned on every call so cached state stays intact even
    if a caller mutates the result.
    """
    return list(
        _parse_proxy_networks_cached(_normalize_proxy_entries(trusted_proxies))
    )


def _collapse_mapped(
    ip: _IPvXAddress,
) -> _IPvXAddress:
    """Collapse an IPv4-mapped IPv6 address to its IPv4 form.

    ``::ffff:1.2.3.4`` -> ``1.2.3.4``. Non-mapped addresses pass through
    unchanged. This lets a trusted IPv4 proxy reached over a dual-stack IPv6
    socket still match an IPv4 network entry.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped
    return ip


def _ip_in_networks(
    ip: _IPvXAddress,
    networks: Iterable[_IPvXNetwork],
) -> bool:
    """True iff ``ip`` (or its IPv4-mapped form) is contained in any network."""
    candidates = (ip, _collapse_mapped(ip))
    for net in networks:
        for cand in candidates:
            if cand.version == net.version and cand in net:
                return True
    return False


def resolve_client_ip(
    request: Request,
    trusted_proxies: Iterable[str],
) -> str:
    """Resolve the real client IP for ``request``.

    Parameters
    ----------
    request:
        The incoming request. Only ``request.client.host`` (the immediate TCP
        peer) and ``request.headers["x-forwarded-for"]`` are read.
    trusted_proxies:
        Iterable of trusted proxy addresses / CIDR ranges (e.g.
        :attr:`engine.config.settings.trusted_proxies_set`).

    Returns
    -------
    str
        The best-effort real client IP. Falls back to the raw peer (or
        :data:`UNKNOWN_CLIENT`) when no trusted proxy is in play or the
        ``X-Forwarded-For`` chain is absent / entirely trusted.
    """
    peer: str | None = None
    if request.client is not None:
        peer = request.client.host
    if not peer:
        return UNKNOWN_CLIENT

    try:
        peer_ip: _IPvXAddress | None = ipaddress.ip_address(peer)
    except ValueError:
        # Peer is not a parseable IP (e.g. a UDS hostname). Return it as-is —
        # we cannot reason about proxy chains for non-IP peers.
        return peer

    networks = parse_proxy_networks(trusted_proxies)

    # If the immediate peer is NOT a trusted proxy, it *is* the client.
    # Never inspect X-Forwarded-For in this case — a client could spoof it.
    assert peer_ip is not None
    if not _ip_in_networks(peer_ip, networks):
        return peer

    # Peer is trusted: walk X-Forwarded-For right-to-left, skipping any hops
    # that are themselves trusted proxies. The first untrusted hop is the
    # genuine client. Each trusted proxy appends the prior hop, so the
    # rightmost *untrusted* entry cannot have been injected by the client.
    forwarded = request.headers.get("x-forwarded-for", "")
    # Walk the chain right-to-left, but cap the number of inspected hops so a
    # pathologically long (or hostile) XFF header cannot force unbounded work.
    # Each trusted proxy appends exactly one hop, so legitimate chains stay
    # well within MAX_XFF_HOPS; a client lying past the cap cannot be reached
    # within the trusted prefix, so we fall back to the peer address.
    for index, raw in enumerate(reversed(forwarded.split(","))):
        if index >= MAX_XFF_HOPS:
            break
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            hop_ip: _IPvXAddress | None = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if _ip_in_networks(hop_ip, networks):
            continue
        return candidate

    # Either no XFF header was present or every hop was a trusted proxy
    # (e.g. a single-hop proxy with no client info). Fall back to the peer.
    return peer
