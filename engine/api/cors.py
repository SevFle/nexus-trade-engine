"""CORS origin normalisation and allowlist matching.

The browser ``Origin`` / ``Referer`` header and operator-configured
``cors_origins`` allowlist entries are matched by string equality in most
CORS implementations (including Starlette's ``CORSMiddleware``).  Origins are
*case-insensitive* by RFC 6454 (scheme and host) and carry no path, so a naive
exact comparison is both brittle and a security smell:

  * an operator who enters ``"https://Example.com/"`` (trailing slash, mixed
    case) would silently *never* match a browser origin of
    ``"https://example.com"`` — a fail-closed misconfiguration; and
  * a hand-rolled matcher that does substring/``startswith`` checks without
    normalising first is trivially bypassed: ``"https://evil.com"`` is a
    "prefix" of ``"https://evil.com.attacker.io"`` and a "suffix" relative to
    ``"https://not-evil.com"``.

This module provides a single canonical normaliser (:func:`normalize_origin`)
plus helpers that pre-normalise an allowlist (:func:`normalize_origin_allowlist`)
and match an incoming header value against it (:func:`is_origin_allowed`).
All three compare *only* canonical ``scheme://host[:port]`` strings, with
scheme and host lower-cased and any trailing slash / path stripped, so the
three classic bypass vectors — trailing slash, upper-case scheme, mixed-case
host — collapse to the same canonical form on both sides of the comparison.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Mapping

_DEFAULT_HTTP_PORT = 80
_DEFAULT_HTTPS_PORT = 443


def normalize_origin(origin: str | None) -> str | None:
    """Return the canonical form of a CORS *origin*, or ``None`` if invalid.

    Canonicalisation:
      * parse with :func:`urllib.parse.urlparse`;
      * lower-case the **scheme** and **host** (case-insensitive per RFC 6454);
      * drop any **path** and **trailing slash** (an origin is ``scheme://host``);
      * preserve an explicit numeric **port**, but drop a port that is the
        scheme default (``:80`` for http, ``:443`` for https) so an explicit
        default port matches a bare ``scheme://host`` allowlist entry;
      * canonicalise Internationalised Domain Names (IDN) to ASCII Punycode
        (``münchen.de`` → ``xn--mnchen-3ya.de``);
      * return ``None`` for anything without a scheme + host (e.g. ``null``,
        ``"file://"``, bare hostnames, or entries with a non-numeric port).

    Examples:
      >>> normalize_origin("https://Example.com/")
      'https://example.com'
      >>> normalize_origin("HTTP://LocalHost:3000")
      'http://localhost:3000'
      >>> normalize_origin("null") is None
      True
    """
    if not isinstance(origin, str):
        return None
    raw = origin.strip()
    if not raw or raw.lower() == "null":
        return None

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    # ``parsed.hostname`` is already lower-cased by urlparse, but normalise
    # explicitly so the canonical form is independent of that implementation
    # detail (and survives a future switch to ``netloc``).
    hostname = parsed.hostname
    if hostname:
        hostname = hostname.lower()
    if not scheme or not hostname:
        return None

    # Canonicalise Internationalised Domain Names (IDN) to their ASCII
    # "Punycode" form (RFC 3490) so that a Unicode origin such as
    # ``https://münchen.de`` and its allowlist entry ``https://xn--mnchen-3ya.de``
    # collapse to a single canonical string and compare equal.  This also
    # neutralises homograph confusion: ``exámple.com`` encodes to a distinct
    # ``xn--exmple-…`` label and therefore cannot match ``example.com``.
    # A hostname that cannot be IDNA-encoded is not a valid origin, and a
    # canonicalisation failure must never crash the request pipeline.
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None

    # ``parsed.port`` raises ``ValueError`` for a non-numeric port
    # (``https://host:abc``). Treat that as an invalid origin rather than
    # propagating, so a single malformed allowlist entry cannot crash the
    # whole request pipeline.
    try:
        port = parsed.port
    except ValueError:
        return None

    # Drop a port that is the scheme default so an explicit ``:443`` over
    # https (or ``:80`` over http) matches a bare ``scheme://host`` allowlist
    # entry.  Without this, an operator who allow-lists ``https://app.test``
    # would silently deny a browser that appends ``:443`` to the Origin.
    if (
        (scheme == "http" and port == _DEFAULT_HTTP_PORT)
        or (scheme == "https" and port == _DEFAULT_HTTPS_PORT)
    ):
        port = None

    netloc = hostname if port is None else f"{hostname}:{port}"
    return f"{scheme}://{netloc}"


def normalize_origin_allowlist(origins: list[str] | None) -> list[str]:
    """Pre-normalise an allowlist, dropping duplicates and invalid entries.

    Order is preserved (first occurrence wins) so the resulting list is a
    stable, canonical view suitable for handing to ``CORSMiddleware``.
    """
    canonical: list[str] = []
    seen: set[str] = set()
    for entry in origins or []:
        normalized = normalize_origin(entry)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        canonical.append(normalized)
    return canonical


def get_header_case_insensitive(
    headers: Mapping[str, str],
    name: str,
) -> str | None:
    """Fetch *name* from *headers* regardless of header-name casing.

    HTTP header names are case-insensitive (RFC 9110 §5.1).  Naive lookups
    like ``headers.get('origin') or headers.get('Origin')`` are therefore
    fragile: an ASGI/WSGI server that surfaces the header as ``"ORIGIN"`` (or
    any other casing) silently bypasses the check.  This helper builds a
    case-insensitive view via a dict comprehension over ``headers.items()``
    and looks up the lower-cased name, so every casing resolves to one value.
    """
    target = name.lower()
    lowered = {key.lower(): value for key, value in headers.items()}
    return lowered.get(target)


def is_origin_allowed(
    origin: str | None,
    allowlist: list[str] | None,
) -> bool:
    """Return ``True`` iff *origin* canonicalises to an allowlist entry.

    Both sides are run through :func:`normalize_origin` before comparison, so
    the trailing-slash / upper-case-scheme / mixed-case-host vectors cannot
    produce a spurious match (or a spurious miss).  An invalid / ``None``
    origin is never allowed, and an empty allowlist denies everything — the
    safe default for a closed CORS policy.
    """
    normalized = normalize_origin(origin)
    if normalized is None:
        return False
    return normalized in set(normalize_origin_allowlist(allowlist))


def is_origin_header_allowed(
    headers: Mapping[str, str],
    allowlist: list[str] | None,
) -> bool:
    """Read the ``Origin`` header (case-insensitively) and match the allowlist.

    Combines :func:`get_header_case_insensitive` (so an oddly-cased
    ``Origin`` header is still read) with :func:`is_origin_allowed` (so the
    value is matched in canonical form).  Absent header → not allowed.
    """
    origin = get_header_case_insensitive(headers, "origin")
    return is_origin_allowed(origin, allowlist)


__all__ = [
    "get_header_case_insensitive",
    "is_origin_allowed",
    "is_origin_header_allowed",
    "normalize_origin",
    "normalize_origin_allowlist",
]
