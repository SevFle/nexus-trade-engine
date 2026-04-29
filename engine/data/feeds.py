"""Backwards-compatible shim that delegates to :mod:`engine.data.providers`.

Earlier code paths imported :class:`MarketDataProvider` and
``get_data_provider`` from this module. Those names are kept so existing
callers (backtest runner, plugins SDK, tests) keep working while the new
:mod:`engine.data.providers` package is the canonical home.

New code should import from :mod:`engine.data.providers` directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

from engine.data.providers.registry import get_registry
from engine.data.providers.yahoo import YahooDataProvider

if TYPE_CHECKING:
    import pandas as pd

    from engine.data.providers.base import IDataProvider

logger = structlog.get_logger()


class MarketDataProvider(ABC):
    """Legacy abstract base. Concrete impls now live under
    :mod:`engine.data.providers`; this class stays so test doubles and
    plugins implementing the old shape keep working.
    """

    @abstractmethod
    async def get_latest_price(self, symbol: str) -> float | None:
        ...

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        ...

    @abstractmethod
    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        ...


class _LegacyAdapter(MarketDataProvider):
    """Wrap a new :class:`IDataProvider` so it satisfies the legacy ABC."""

    def __init__(self, inner: IDataProvider) -> None:
        self._inner = inner

    async def get_latest_price(self, symbol: str) -> float | None:
        return await self._inner.get_latest_price(symbol)

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        return await self._inner.get_ohlcv(symbol, period=period, interval=interval)

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        return await self._inner.get_multiple_prices(symbols)


def get_data_provider(provider_name: str = "yahoo") -> MarketDataProvider:
    """Return a legacy-compatible provider for ``provider_name``.

    Defaults to Yahoo (no key required) so callers without a configured
    registry keep working in development. Production callers should use the
    :class:`engine.data.providers.DataProviderRegistry` directly.
    """
    if provider_name == "yahoo":
        return _LegacyAdapter(YahooDataProvider())

    registry = get_registry()
    inner = registry.get(provider_name)
    if inner is None:
        raise ValueError(
            f"Unknown data provider: {provider_name}. "
            "Configure it via engine.data.providers.configure_from_file()."
        )
    return _LegacyAdapter(inner)
