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

# Only fields whose name appears in this set may carry ``${VAR}``
# placeholders. This keeps env expansion away from ``priority``,
# ``asset_classes``, and other structural fields whose value should
# never come from an arbitrary env var.
EXPANDABLE_OPTION_KEYS: frozenset[str] = frozenset(
    {"api_key", "api_secret", "environment", "passphrase", "account_id"}
)


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


def _expand_env_vars(value: str, *, key: str, provider: str) -> str:
    """Replace ``${VAR}`` references with environment values.

    Raises if a referenced variable is unset — silent expansion to ``""``
    masks misconfigurations (e.g. forgotten ``POLYGON_API_KEY``) and was
    flagged by adversarial review as security-relevant.
    """
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        var = match.group(1)
        env_value = os.environ.get(var)
        if env_value is None:
            missing.append(var)
            return ""
        return env_value

    expanded = ENV_VAR_PATTERN.sub(repl, value)
    if missing:
        raise ValueError(
            f"data_providers.{provider}.{key} references unset env var(s): "
            f"{', '.join(sorted(set(missing)))}"
        )
    return expanded


def parse_config(payload: dict) -> list[ProviderConfig]:
    if not isinstance(payload, dict):
        raise ValueError("data_providers config must be a mapping")
    providers = payload.get("data_providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("data_providers root must be a mapping")

    out: list[ProviderConfig] = []
    for name, body in providers.items():
        if not isinstance(body, dict):
            raise ValueError(f"data_providers.{name} must be a mapping")
        enabled = bool(body.get("enabled", True))
        priority = int(body.get("priority", 100))
        asset_classes = list(body.get("asset_classes") or [])

        options: dict[str, str] = {}
        for option_key, value in body.items():
            if option_key in {"enabled", "priority", "asset_classes"}:
                continue
            text = str(value)
            if option_key in EXPANDABLE_OPTION_KEYS:
                text = _expand_env_vars(text, key=option_key, provider=name)
            elif ENV_VAR_PATTERN.search(text):
                raise ValueError(
                    f"data_providers.{name}.{option_key} is not in the env-expandable "
                    f"allowlist: {sorted(EXPANDABLE_OPTION_KEYS)}"
                )
            options[option_key] = text

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
