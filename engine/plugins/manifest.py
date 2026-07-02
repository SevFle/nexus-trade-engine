"""
Strategy Manifest — declarative metadata for every plugin.

Parsed from strategy.manifest.yaml. Controls sandboxing, dependencies,
network whitelist, config schema, and marketplace listing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from engine.plugins.restricted_importer import extract_hostnames


class ResourceLimits(BaseModel):
    max_memory: str = "512MB"
    gpu: str = "none"  # none | optional | required
    max_cpu_seconds: int = 30


class NetworkConfig(BaseModel):
    allowed_endpoints: list[str] = Field(default_factory=list)

    @field_validator("allowed_endpoints")
    @classmethod
    def _validate_endpoint_format(cls, value: list[str]) -> list[str]:
        """Reject malformed ``allowed_endpoints`` at deserialization time.

        The network allowlist is **host-granular**: a port (``host:8080``) or
        path (``host/v1``) component would be silently ignored by the matching
        logic in :class:`~engine.plugins.sandboxed_http.SandboxedHttpClient`
        and the httpx ``send`` hook, giving a false sense of restriction.  Such
        entries are therefore rejected with :class:`ValueError` (surfaced by
        pydantic as :class:`~pydantic.ValidationError`) the moment the manifest
        is parsed — i.e. *before* the entry ever reaches the sandbox.

        Bare hostnames (``api.example.com``) and full URLs
        (``https://api.example.com``) are accepted unchanged; the original
        list is preserved so the manifest remains a faithful declarative
        record.  Downstream consumers normalise again via
        :func:`extract_hostnames`.
        """
        extract_hostnames(value)  # raises ValueError on malformed entries
        return value


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
