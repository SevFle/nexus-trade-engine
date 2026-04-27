"""Load provider configuration from a YAML file with env-var expansion.

Resolution order for each setting:

1.  ``${ENV_VAR}`` placeholders inside the YAML are substituted with values
    from the environment.
2.  An adapter-specific env var (e.g. ``NEXUS_POLYGON_API_KEY``) overrides
    the YAML value when set.

This keeps a single YAML file authoritative for *what* providers exist
(priority, asset classes, enabled), while letting operators inject
secrets via the environment.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

from engine.data.providers.alpaca_data import AlpacaDataProvider
from engine.data.providers.base import AssetClass, IDataProvider
from engine.data.providers.binance import BinanceDataProvider
from engine.data.providers.coingecko import CoinGeckoDataProvider
from engine.data.providers.oanda import OandaDataProvider
from engine.data.providers.polygon import PolygonDataProvider
from engine.data.providers.registry import (
    DataProviderRegistry,
    ProviderRegistration,
    get_registry,
)
from engine.data.providers.yahoo import YahooDataProvider

logger = structlog.get_logger()

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    enabled: bool = True
    priority: int = 100
    asset_classes: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)

    def resolved_asset_classes(self) -> frozenset[AssetClass]:
        out: set[AssetClass] = set()
        for raw in self.asset_classes:
            try:
                out.add(AssetClass(raw))
            except ValueError:
                logger.warning(
                    "data_provider.config.unknown_asset_class",
                    provider=self.name,
                    value=raw,
                )
        return frozenset(out)


def _expand_env_vars(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return ENV_VAR_PATTERN.sub(repl, value)


def _coerce(value: object) -> object:
    if isinstance(value, str):
        return _expand_env_vars(value)
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    return value


def parse_config(payload: dict) -> list[ProviderConfig]:
    raw = ((_coerce(payload) or {}) or {})
    if not isinstance(raw, dict):
        raise ValueError("data_providers config must be a mapping")
    providers = raw.get("data_providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("data_providers root must be a mapping")

    out: list[ProviderConfig] = []
    for name, body in providers.items():
        if not isinstance(body, dict):
            raise ValueError(f"data_providers.{name} must be a mapping")
        enabled = bool(body.get("enabled", True))
        priority = int(body.get("priority", 100))
        asset_classes = list(body.get("asset_classes") or [])
        options = {
            k: str(v)
            for k, v in body.items()
            if k not in {"enabled", "priority", "asset_classes"}
        }
        out.append(
            ProviderConfig(
                name=name,
                enabled=enabled,
                priority=priority,
                asset_classes=asset_classes,
                options=options,
            )
        )
    return out


def load_config(path: str | Path) -> list[ProviderConfig]:
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"data providers config not found: {path}")
    with file.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return parse_config(payload)


def _env_override(provider: str, key: str) -> str | None:
    """Look up ``NEXUS_<PROVIDER>_<KEY>`` and fall back to ``<PROVIDER>_<KEY>``."""
    candidates = (
        f"NEXUS_{provider.upper()}_{key.upper()}",
        f"{provider.upper()}_{key.upper()}",
    )
    for var in candidates:
        value = os.environ.get(var)
        if value:
            return value
    return None


def _opt(cfg: ProviderConfig, key: str) -> str:
    direct = cfg.options.get(key, "")
    override = _env_override(cfg.name, key)
    return override or direct


def build_provider(cfg: ProviderConfig) -> IDataProvider:
    name = cfg.name
    if name == "yahoo":
        return YahooDataProvider()
    if name == "polygon":
        return PolygonDataProvider(api_key=_opt(cfg, "api_key"))
    if name == "alpaca":
        return AlpacaDataProvider(
            api_key=_opt(cfg, "api_key"),
            api_secret=_opt(cfg, "api_secret"),
        )
    if name == "binance":
        return BinanceDataProvider(
            api_key=_opt(cfg, "api_key") or None,
            api_secret=_opt(cfg, "api_secret") or None,
        )
    if name == "coingecko":
        return CoinGeckoDataProvider()
    if name == "oanda":
        return OandaDataProvider(
            api_key=_opt(cfg, "api_key"),
            environment=_opt(cfg, "environment") or "practice",
        )
    raise ValueError(f"Unknown provider: {name}")


def configure_registry(
    configs: list[ProviderConfig],
    registry: DataProviderRegistry | None = None,
) -> DataProviderRegistry:
    target = registry or get_registry()
    for cfg in configs:
        if not cfg.enabled:
            continue
        try:
            provider = build_provider(cfg)
        except Exception as exc:
            logger.exception(
                "data_provider.config.build_failed",
                provider=cfg.name,
                error=str(exc),
            )
            continue
        target.register(
            ProviderRegistration(
                provider=provider,
                priority=cfg.priority,
                asset_classes=cfg.resolved_asset_classes(),
                enabled=cfg.enabled,
            )
        )
        logger.info(
            "data_provider.registered",
            provider=cfg.name,
            priority=cfg.priority,
            asset_classes=list(cfg.asset_classes),
        )
    return target


def configure_from_file(
    path: str | Path,
    registry: DataProviderRegistry | None = None,
) -> DataProviderRegistry:
    return configure_registry(load_config(path), registry)
