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
  the genuine origin.
* The right-to-left walk is capped at :data:`MAX_XFF_HOPS` entries so a
  maliciously long header cannot be used to degrade resolution.

This module is intentionally dependency-free (no FastAPI imports at runtime) so
it can be unit-tested in isolation with lightweight request doubles.
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

#: Maximum number of ``X-Forwarded-For`` hops inspected when walking the chain
#: right-to-left. A well-formed deployment has a handful of proxies; any chain
#: longer than this is either misconfigured or a deliberate attempt to exhaust
#: the resolver, so the walk is truncated and we fall back to the trusted peer.
MAX_XFF_HOPS = 16

_logger = structlog.get_logger(__name__)

_IPvXAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPvXNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=128)
def _parse_proxy_networks_cached(
    trusted_proxies: tuple[str, ...],
) -> tuple[_IPvXNetwork, ...]:
    """Cached core of :func:`parse_proxy_networks`.

    The cache is keyed by a sorted, de-duplicated tuple of the raw proxy
    entries (see :func:`parse_proxy_networks`) so repeated per-request parsing
    of the same static configuration is avoided. Returns an immutable tuple so
    callers cannot mutate a shared cached value.
    """
    networks: list[_IPvXNetwork] = []
    for raw in trusted_proxies:
        if raw is None:
            continue
        entry = raw.strip()
        if not entry:
            continue
        try:
            # ip_network accepts both bare hosts (-> /32 or /128) and CIDR
            # ranges. ``strict=False`` tolerates host bits set in a CIDR.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            # Log malformed operator config rather than silently swallowing it,
            # so misconfigurations surface in observability tooling.
            _logger.warning(
                "ip_utils.invalid_proxy_entry",
                entry=entry,
                error=str(exc),
            )
            continue
    return tuple(networks)


def parse_proxy_networks(
    trusted_proxies: Iterable[str],
) -> list[_IPvXNetwork]:
    """Parse an iterable of trusted-proxy entries into IP networks.

    Each entry may be a bare address (``"10.0.0.1"``) or a CIDR range
    (``"10.0.0.0/8"``). Blank entries are ignored and unparseable values emit
    a structured warning (via :data:`_logger`) and are skipped rather than
    raising, so a malformed operator config cannot break IP resolution for the
    whole service.

    Parsing is memoized via :func:`_parse_proxy_networks_cached`, keyed by a
    sorted, de-duplicated tuple of the (stripped) entries, so the same trusted
    proxy set is only parsed once per process.
    """
    # Normalize to a hashable, deterministic cache key. Sorting the de-duplicated
    # set makes the key order-independent, so a set input (e.g.
    # ``settings.trusted_proxies_set``) hits the cache regardless of iteration
    # order.
    key = tuple(sorted({str(p).strip() for p in trusted_proxies if p and str(p).strip()}))
    return list(_parse_proxy_networks_cached(key))


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
        ``X-Forwarded-For`` chain is absent / entirely trusted / longer than
        :data:`MAX_XFF_HOPS`.
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
    hops_examined = 0
    for raw in reversed(forwarded.split(",")):
        # Cap the walk: a chain longer than MAX_XFF_HOPS is either misconfigured
        # or an attempt to exhaust the resolver. Truncate and fall back.
        if hops_examined >= MAX_XFF_HOPS:
            break
        hops_examined += 1
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

    # Either no XFF header was present, every hop was a trusted proxy, or the
    # chain exceeded MAX_XFF_HOPS before an untrusted hop was found. Fall back
    # to the peer.
    return peer
