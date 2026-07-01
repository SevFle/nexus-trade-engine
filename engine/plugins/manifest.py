"""
Strategy Manifest — declarative metadata for every plugin.

Parsed from strategy.manifest.yaml. Controls sandboxing, dependencies,
network whitelist, config schema, and marketplace listing.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def normalize_endpoint(entry: str) -> str:
    """Canonicalise a single ``allowed_endpoints`` entry to a bare hostname.

    Accepts any of the convenient forms a manifest author might write and
    reduces them all to the same canonical, lowercase hostname:

      * bare hostname — ``api.example.com``
      * schemeless URL — ``//api.example.com`` or ``api.example.com/v1``
      * full URL — ``https://api.example.com``

    The hostname is returned lowercased for case-normalisation consistency
    (DNS hostnames are case-insensitive, and the sandbox's allowlist matchers
    compare against this canonical form).

    Entries that carry a port, path, query, fragment or userinfo are rejected
    with a clear ``ValueError`` at manifest-load time.  The sandbox's
    host-allowlist matches on *hostnames only*; a port/path component would
    therefore silently never match (request hosts carry no port/path) and must
    be surfaced as an explicit configuration error rather than a silent
    network-access failure.
    """
    if not isinstance(entry, str):
        raise TypeError("allowed_endpoints entry must be a string")
    raw = entry.strip()
    if not raw:
        raise ValueError("allowed_endpoints entry must not be empty")
    # Prepend ``//`` to schemeless entries so ``urlparse`` treats the leading
    # segment as the netloc/hostname.  Without this a bare URL such as
    # ``api.example.com/v1`` is parsed with ``api.example.com`` as the
    # *scheme* and the hostname comes back as ``None``.
    candidate = raw
    if "://" not in candidate and not candidate.startswith("//"):
        candidate = "//" + candidate
    parsed = urlparse(candidate)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(
            f"allowed_endpoints entry {entry!r} has no parseable hostname"
        )
    # Reject anything beyond a bare hostname so the host-only matcher never
    # silently fails to match.  Each check produces a targeted, actionable
    # error message.
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            f"allowed_endpoints entry {entry!r} must not include userinfo; "
            "specify the hostname only"
        )
    if parsed.port is not None:
        raise ValueError(
            f"allowed_endpoints entry {entry!r} must not include a port "
            f"({parsed.port}); specify the hostname only"
        )
    if parsed.path and parsed.path != "/":
        raise ValueError(
            f"allowed_endpoints entry {entry!r} must not include a path "
            f"({parsed.path!r}); specify the hostname only"
        )
    if parsed.query:
        raise ValueError(
            f"allowed_endpoints entry {entry!r} must not include a query "
            f"string ({parsed.query!r}); specify the hostname only"
        )
    if parsed.fragment:
        raise ValueError(
            f"allowed_endpoints entry {entry!r} must not include a fragment "
            f"({parsed.fragment!r}); specify the hostname only"
        )
    return hostname.lower()


def host_matches_allowlist(
    host: Any,
    allowed_endpoints: list[str] | tuple[str, ...] | None,
) -> bool:
    """Return ``True`` iff *host* is permitted by the endpoint allowlist.

    A host matches when it exactly equals an allowlist entry or is a subdomain
    of one (e.g. ``api.foo.com`` for a ``foo.com`` entry).  Comparison is
    case-insensitive: both sides are lowercased so a mixed-case request host
    matches a canonical (lowercase) entry even if the entry was supplied
    directly (bypassing the manifest validator).

    With an empty allowlist (no network declared) every host is rejected —
    matching the ``SandboxedHttpClient`` semantics where an empty whitelist
    blocks all network access.
    """
    if not allowed_endpoints:
        return False
    if host is None:
        return False
    name = str(host).lower()
    if not name:
        return False
    return any(
        name == ep.lower() or name.endswith(f".{ep.lower()}")
        for ep in allowed_endpoints
    )



class ResourceLimits(BaseModel):
    max_memory: str = "512MB"
    gpu: str = "none"  # none | optional | required
    max_cpu_seconds: int = 30


class NetworkConfig(BaseModel):
    allowed_endpoints: list[str] = Field(default_factory=list)

    @field_validator("allowed_endpoints", mode="after")
    @classmethod
    def _normalize_allowed_endpoints(cls, value: list[str]) -> list[str]:
        """Normalise every endpoint to a bare, lowercase hostname at load time.

        This is the single chokepoint where manifest-declared endpoints are
        canonicalised *before* they reach the sandbox's host-allowlist.  Doing
        it here (rather than ad hoc at each enforcement site) guarantees that
        every matcher — ``RestrictedImporter``, ``SandboxedHttpClient`` and the
        httpx ``send`` hook — compares against identical, well-formed values.
        """
        return [normalize_endpoint(ep) for ep in value]


class StrategyManifest(BaseModel):
    """Full manifest schema matching strategy.manifest.yaml."""

    # ── Identity ──
    id: str
    name: str
    version: str
    author: str = "unknown"
    description: str = ""
    license: str = "MIT"
    min_engine_version: str = "0.1.0"

    # ── Runtime ──
    runtime: str = "python:3.11"
    dependencies: list[str] = Field(default_factory=list)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)

    # ── Bundled artifacts (model weights, prompts, etc.) ──
    artifacts: list[str] = Field(default_factory=list)

    # ── Network access (sandboxed whitelist) ──
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    # ── User-configurable parameters ──
    config_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )

    # ── Marketplace metadata ──
    marketplace: dict[str, Any] | None = Field(default=None)

    # ── Data requirements ──
    data_feeds: list[str] = Field(
        default_factory=lambda: ["ohlcv"],
        description="Required data feeds: ohlcv, news, sentiment, order_book, macro",
    )
    min_history_bars: int = 50
    watchlist: list[str] = Field(
        default_factory=list, description="Default symbols. Empty = user chooses."
    )

    def requires_network(self) -> bool:
        return len(self.network.allowed_endpoints) > 0

    def requires_gpu(self) -> bool:
        return self.resources.gpu == "required"
