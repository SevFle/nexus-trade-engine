"""Configuration for the Nexus MCP server.

Settings are read from environment variables prefixed ``NEXUS_MCP_`` (e.g.
``NEXUS_MCP_AUTH_REQUIRED=false``) and/or the project ``.env`` file. Keeping
the MCP config separate from :mod:`engine.config` lets the server run as a
standalone stdio process without pulling in the full API/DB surface.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPServerSettings(BaseSettings):
    """Runtime configuration for ``engine.mcp``."""

    model_config = SettingsConfigDict(
        env_prefix="NEXUS_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Identity ──
    server_name: str = "nexus-mcp-server"
    server_version: str = "0.1.0"
    instructions: str = (
        "Nexus Trade Engine MCP server. Use the available tools to run "
        "backtests, inspect portfolios, list strategies, and query market "
        "data and cost models. Read-only operations are safe; backtests are "
        "compute-only and never place live orders."
    )

    # ── Transport ──
    transport: str = "stdio"  # stdio | http
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    http_path: str = "/mcp"
    http_log_level: str = "info"

    # ── Auth / RBAC ──
    auth_required: bool = True
    # Role granted to anonymous/local sessions when auth is disabled.
    default_role: str = "viewer"
    # Optional JWT/engine token supplied to the server process directly.
    # Primarily for stdio deployments where the client cannot set request
    # headers — the token is then forwarded into engine RBAC.
    token: str = ""
    # JSON map of ``{"<static-api-key>": "<role>"}`` for token auth that does
    # not require a database lookup. Useful for service-to-service MCP.
    static_api_keys: str = ""

    # ── Rate limiting (per authenticated principal) ──
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30

    # ── Result safety ──
    # Soft cap on the number of tokens an MCP response may consume so we do
    # not blow out an assistant's context window. Estimation is ~4 chars/token.
    result_token_budget: int = 24_000
    default_page_size: int = 50
    max_page_size: int = 500

    # ── Backtest execution ──
    # 0 disables intra-run progress; >0 emits a progress notification every N
    # equity-curve points (requires runner instrumentation).
    backtest_progress_interval: int = 0
    backtest_max_bars: int = 50_000
    backtest_default_provider: str = "yahoo"

    @property
    def static_api_keys_map(self) -> dict[str, str]:
        """Parse :attr:`static_api_keys` into a typed ``token -> role`` map.

        Malformed payloads are silently ignored so a bad operator env var
        cannot brick the server.
        """
        raw = (self.static_api_keys or "").strip()
        if not raw:
            return {}
        try:
            parsed: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}


mcp_settings = MCPServerSettings()


__all__ = ["MCPServerSettings", "mcp_settings"]
