"""Pluggable market data provider interface.

Defines the abstract :class:`IDataProvider` contract that every concrete
adapter (Yahoo, Polygon, Alpaca, Binance, CoinGecko, OANDA, …) implements,
plus the capability model the registry uses to route requests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pandas as pd

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    OPTIONS = "options"
    FOREX = "forex"
    CRYPTO = "crypto"
    FUTURES = "futures"


class HealthStatus(StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True)
class RateLimit:
    """Provider rate-limit declaration. ``requests_per_minute=0`` means unlimited."""

    requests_per_minute: int = 0
    burst: int = 1


@dataclass(frozen=True)
class DataProviderCapability:
    """Static description of what a provider supports.

    Used by the registry to decide which providers can satisfy a request.
    """

    name: str
    asset_classes: frozenset[AssetClass]
    supports_realtime: bool = False
    supports_options_chain: bool = False
    supports_orderbook: bool = False
    supports_streaming: bool = False
    max_history_days: int | None = None
    min_interval: str = "1d"
    rate_limit: RateLimit = field(default_factory=RateLimit)
    requires_api_key: bool = False


@dataclass(frozen=True)
class HealthCheckResult:
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str = ""


class ProviderError(Exception):
    """Base for any provider-side failure."""


class TransientProviderError(ProviderError):
    """Recoverable: rate-limited, network blip, 5xx — retried by ``call_with_retry``,
    then failed-over to the next provider by the registry."""


class FatalProviderError(ProviderError):
    """Non-recoverable for this provider, but the registry will continue trying
    the next candidate. Use for bad credentials, unsupported assets, malformed
    schema, or capability gaps. The registry surfaces the *last* fatal as part
    of :class:`NoProviderAvailableError` when nothing succeeds."""


class CapabilityNotSupportedError(FatalProviderError):
    """Specific subclass for when a provider lacks a requested capability.

    Surfaced separately so callers can distinguish "no one supports this op"
    from "all providers failed". The registry pre-filters candidates by
    capability so this rarely escapes; when it does, every candidate lacked
    the feature.
    """


SYMBOL_PATTERN = r"^[A-Za-z0-9._\-/=^]{1,32}$"


class IDataProvider(ABC):
    """Common interface every data provider must implement.

    Concrete adapters return :class:`pandas.DataFrame` with the lowercase
    canonical columns ``(open, high, low, close, volume)`` indexed by an
    ascending UTC :class:`pandas.DatetimeIndex`.
    """

    capability: DataProviderCapability

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return historical OHLCV bars for ``symbol``."""

    @abstractmethod
    async def get_latest_price(self, symbol: str) -> float | None:
        """Return the most recent traded price, or ``None`` if unavailable."""

    @abstractmethod
    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        """Batch latest-price fetch. Missing symbols are omitted from the result."""

    @abstractmethod
    async def get_options_chain(self, symbol: str, expiry: str | None = None) -> pd.DataFrame:
        """Return options chain. Empty DataFrame when not supported."""

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        """Return L2 order book. Empty DataFrame when not supported."""

    @abstractmethod
    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        """Async iterator yielding ``{symbol: price}`` ticks. Raise when unsupported."""

    @abstractmethod
    async def health_check(self) -> HealthCheckResult:
        """Lightweight liveness probe used by status dashboards."""

    @property
    def name(self) -> str:
        return self.capability.name
