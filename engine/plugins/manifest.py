"""
Strategy Manifest — declarative metadata for every plugin.

Parsed from strategy.manifest.yaml. Controls sandboxing, dependencies,
network whitelist, config schema, and marketplace listing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ResourceLimits(BaseModel):
    max_memory: str = "512MB"
    gpu: str = "none"  # none | optional | required
    max_cpu_seconds: int = 30


class NetworkConfig(BaseModel):
    allowed_endpoints: list[str] = Field(default_factory=list)


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
    watchlist: list[str] = Field(default_factory=list, description="Default symbols. Empty = user chooses.")

    def requires_network(self) -> bool:
        return len(self.network.allowed_endpoints) > 0

    def requires_gpu(self) -> bool:
        return self.resources.gpu == "required"
