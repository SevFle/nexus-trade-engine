"""Routing registry that picks an :class:`IDataProvider` per request.

Order of operations for any read:

1.  Resolve candidate providers for the requested ``asset_class`` ordered
    by configured priority (lower number = higher priority).
2.  Try them in order; on :class:`TransientProviderError` move to the next
    candidate. :class:`FatalProviderError` propagates.
3.  If nothing succeeds, raise :class:`NoProviderAvailable`.

The registry itself is async-safe and can be mutated at runtime (e.g.
hot-reload from YAML) without restarting the engine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from engine.data.providers.base import (
    AssetClass,
    FatalProviderError,
    HealthCheckResult,
    HealthStatus,
    IDataProvider,
    ProviderError,
    TransientProviderError,
)

if TYPE_CHECKING:
    import pandas as pd

logger = structlog.get_logger()


class NoProviderAvailableError(ProviderError):
    """No registered provider could satisfy the request."""


# Backwards-compatible alias.
NoProviderAvailable = NoProviderAvailableError


@dataclass(frozen=True)
class ProviderRegistration:
    provider: IDataProvider
    priority: int = 100
    asset_classes: frozenset[AssetClass] = field(default_factory=frozenset)
    enabled: bool = True

    @property
    def name(self) -> str:
        return self.provider.name


class DataProviderRegistry:
    """Holds the set of configured providers and routes calls to them."""

    def __init__(self) -> None:
        self._registrations: dict[str, ProviderRegistration] = {}
        self._lock = asyncio.Lock()

    def register(self, registration: ProviderRegistration) -> None:
        if registration.name in self._registrations:
            raise ValueError(f"Provider already registered: {registration.name}")
        effective_classes = (
            registration.asset_classes or registration.provider.capability.asset_classes
        )
        self._registrations[registration.name] = ProviderRegistration(
            provider=registration.provider,
            priority=registration.priority,
            asset_classes=frozenset(effective_classes),
            enabled=registration.enabled,
        )

    def deregister(self, name: str) -> None:
        self._registrations.pop(name, None)

    def list_providers(self) -> list[ProviderRegistration]:
        return list(self._registrations.values())

    def get(self, name: str) -> IDataProvider | None:
        reg = self._registrations.get(name)
        return reg.provider if reg else None

    def candidates_for(self, asset_class: AssetClass) -> list[ProviderRegistration]:
        matched = [
            reg
            for reg in self._registrations.values()
            if reg.enabled and asset_class in reg.asset_classes
        ]
        matched.sort(key=lambda r: (r.priority, r.name))
        return matched

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
        asset_class: AssetClass = AssetClass.EQUITY,
    ) -> pd.DataFrame:
        async def call(p: IDataProvider) -> pd.DataFrame:
            return await p.get_ohlcv(symbol, period=period, interval=interval)

        return await self._run_with_failover(asset_class, "get_ohlcv", call, symbol=symbol)

    async def get_latest_price(
        self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY
    ) -> float | None:
        async def call(p: IDataProvider) -> float | None:
            return await p.get_latest_price(symbol)

        try:
            return await self._run_with_failover(
                asset_class, "get_latest_price", call, symbol=symbol
            )
        except NoProviderAvailable:
            return None

    async def get_multiple_prices(
        self, symbols: list[str], asset_class: AssetClass = AssetClass.EQUITY
    ) -> dict[str, float]:
        async def call(p: IDataProvider) -> dict[str, float]:
            return await p.get_multiple_prices(symbols)

        try:
            return await self._run_with_failover(
                asset_class, "get_multiple_prices", call, symbol=",".join(symbols[:5])
            )
        except NoProviderAvailable:
            return {}

    async def get_options_chain(
        self,
        symbol: str,
        expiry: str | None = None,
        asset_class: AssetClass = AssetClass.OPTIONS,
    ) -> pd.DataFrame:
        async def call(p: IDataProvider) -> pd.DataFrame:
            if not p.capability.supports_options_chain:
                raise FatalProviderError(f"{p.name} does not support options chain")
            return await p.get_options_chain(symbol, expiry=expiry)

        return await self._run_with_failover(
            asset_class, "get_options_chain", call, symbol=symbol
        )

    async def get_orderbook(
        self,
        symbol: str,
        depth: int = 20,
        asset_class: AssetClass = AssetClass.CRYPTO,
    ) -> pd.DataFrame:
        async def call(p: IDataProvider) -> pd.DataFrame:
            if not p.capability.supports_orderbook:
                raise FatalProviderError(f"{p.name} does not support orderbook")
            return await p.get_orderbook(symbol, depth=depth)

        return await self._run_with_failover(asset_class, "get_orderbook", call, symbol=symbol)

    async def health(self) -> list[HealthCheckResult]:
        results: list[HealthCheckResult] = []
        for reg in self._registrations.values():
            try:
                results.append(await reg.provider.health_check())
            except Exception as exc:
                results.append(
                    HealthCheckResult(
                        name=reg.name,
                        status=HealthStatus.DOWN,
                        detail=str(exc),
                    )
                )
        return results

    async def _run_with_failover(
        self,
        asset_class: AssetClass,
        method: str,
        call,
        symbol: str = "",
    ):
        candidates = self.candidates_for(asset_class)
        if not candidates:
            raise NoProviderAvailable(
                f"No provider configured for asset_class={asset_class.value}"
            )

        last_exc: BaseException | None = None
        for reg in candidates:
            try:
                return await call(reg.provider)
            except FatalProviderError as exc:
                last_exc = exc
                logger.warning(
                    "data_provider.fatal_skip",
                    provider=reg.name,
                    method=method,
                    symbol=symbol,
                    error=str(exc),
                )
                continue
            except (TransientProviderError, TimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "data_provider.failover",
                    provider=reg.name,
                    method=method,
                    symbol=symbol,
                    error=str(exc),
                )
                continue

        raise NoProviderAvailable(
            f"All providers failed for asset_class={asset_class.value} method={method}: {last_exc}"
        )


_GLOBAL_REGISTRY: DataProviderRegistry | None = None


def get_registry() -> DataProviderRegistry:
    """Process-wide registry singleton. Safe to call before configuration."""
    global _GLOBAL_REGISTRY  # noqa: PLW0603
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = DataProviderRegistry()
    return _GLOBAL_REGISTRY


def reset_registry_for_tests() -> None:
    global _GLOBAL_REGISTRY  # noqa: PLW0603
    _GLOBAL_REGISTRY = None
