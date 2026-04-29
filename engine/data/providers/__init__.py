"""Pluggable market-data provider system.

Public surface:

* :class:`IDataProvider` — interface every adapter implements.
* :class:`DataProviderRegistry` — picks the right adapter per request.
* ``get_registry()`` — process-wide registry singleton.
* ``configure_from_file()`` / ``configure_registry()`` — wire YAML config in.
"""

from __future__ import annotations

from engine.data.providers.alpaca_data import AlpacaDataProvider
from engine.data.providers.base import (
    AssetClass,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    HealthStatus,
    IDataProvider,
    ProviderError,
    RateLimit,
    TransientProviderError,
)
from engine.data.providers.binance import BinanceDataProvider
from engine.data.providers.coingecko import CoinGeckoDataProvider
from engine.data.providers.config import (
    ProviderConfig,
    build_provider,
    configure_from_file,
    configure_registry,
    load_config,
    parse_config,
)
from engine.data.providers.oanda import OandaDataProvider
from engine.data.providers.polygon import PolygonDataProvider
from engine.data.providers.registry import (
    DataProviderRegistry,
    NoProviderAvailable,
    NoProviderAvailableError,
    ProviderRegistration,
    get_registry,
    reset_registry_for_tests,
)
from engine.data.providers.yahoo import YahooDataProvider

__all__ = [
    "AlpacaDataProvider",
    "AssetClass",
    "BinanceDataProvider",
    "CoinGeckoDataProvider",
    "DataProviderCapability",
    "DataProviderRegistry",
    "FatalProviderError",
    "HealthCheckResult",
    "HealthStatus",
    "IDataProvider",
    "NoProviderAvailable",
    "NoProviderAvailableError",
    "OandaDataProvider",
    "PolygonDataProvider",
    "ProviderConfig",
    "ProviderError",
    "ProviderRegistration",
    "RateLimit",
    "TransientProviderError",
    "YahooDataProvider",
    "build_provider",
    "configure_from_file",
    "configure_registry",
    "get_registry",
    "load_config",
    "parse_config",
    "reset_registry_for_tests",
]
