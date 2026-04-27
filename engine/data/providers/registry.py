"""Routing registry that picks an :class:`IDataProvider` per request.

Order of operations for any read:

1.  Resolve candidate providers for the requested ``asset_class`` ordered
    by configured priority (lower number = higher priority). A capability
    predicate further narrows candidates so an adapter that doesn't
    declare ``supports_options_chain`` is never asked for one.
2.  Try them in order. ``FatalProviderError`` (incl. ``CapabilityNotSupportedError``)
    skips to the next candidate but is preserved as ``last_exc`` so the
    final ``NoProviderAvailableError`` still surfaces the cause.
    ``TransientProviderError`` / ``TimeoutError`` likewise fail-over.
3.  If a provider returns an *empty* DataFrame for OHLCV calls, the
    registry treats that as a soft miss and tries the next candidate
    (e.g. Yahoo returning ``[]`` for a delisted symbol still lets us try
    Polygon). The empty result of the last candidate is returned if
    nothing else has data.
4.  If nothing succeeds, raise :class:`NoProviderAvailableError`.

The registry itself is async-safe and can be mutated at runtime (e.g.
hot-reload from YAML) without restarting the engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

import structlog

from engine.data.providers.base import (
    AssetClass,
    CapabilityNotSupportedError,
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

T = TypeVar("T")


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


CapabilityPredicate = Callable[[IDataProvider], bool]


def _is_empty_dataframe(value: object) -> bool:
    """True iff ``value`` is a DataFrame with zero rows."""
    return hasattr(value, "empty") and bool(getattr(value, "empty", False))


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

    def candidates_for(
        self,
        asset_class: AssetClass,
        capability: CapabilityPredicate | None = None,
    ) -> list[ProviderRegistration]:
        matched = [
            reg
            for reg in self._registrations.values()
            if reg.enabled
            and asset_class in reg.asset_classes
            and (capability is None or capability(reg.provider))
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

        return await self._run_with_failover(
            asset_class, "get_ohlcv", call, symbol=symbol, retry_on_empty=True
        )

    async def get_latest_price(
        self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY
    ) -> float | None:
        async def call(p: IDataProvider) -> float | None:
            return await p.get_latest_price(symbol)

        try:
            return await self._run_with_failover(
                asset_class,
                "get_latest_price",
                call,
                symbol=symbol,
                retry_on_empty=True,
            )
        except NoProviderAvailableError:
            return None

    async def get_multiple_prices(
        self, symbols: list[str], asset_class: AssetClass = AssetClass.EQUITY
    ) -> dict[str, float]:
        async def call(p: IDataProvider) -> dict[str, float]:
            return await p.get_multiple_prices(symbols)

        try:
            return await self._run_with_failover(
                asset_class,
                "get_multiple_prices",
                call,
                symbol=",".join(symbols[:5]),
                retry_on_empty=True,
            )
        except NoProviderAvailableError:
            return {}

    async def get_options_chain(
        self,
        symbol: str,
        expiry: str | None = None,
        asset_class: AssetClass = AssetClass.OPTIONS,
    ) -> pd.DataFrame:
        async def call(p: IDataProvider) -> pd.DataFrame:
            return await p.get_options_chain(symbol, expiry=expiry)

        return await self._run_with_failover(
            asset_class,
            "get_options_chain",
            call,
            symbol=symbol,
            capability=lambda p: p.capability.supports_options_chain,
        )

    async def get_orderbook(
        self,
        symbol: str,
        depth: int = 20,
        asset_class: AssetClass = AssetClass.CRYPTO,
    ) -> pd.DataFrame:
        async def call(p: IDataProvider) -> pd.DataFrame:
            return await p.get_orderbook(symbol, depth=depth)

        return await self._run_with_failover(
            asset_class,
            "get_orderbook",
            call,
            symbol=symbol,
            capability=lambda p: p.capability.supports_orderbook,
        )

    async def health(self) -> list[HealthCheckResult]:
        results: list[HealthCheckResult] = []
        for reg in self._registrations.values():
            try:
                results.append(await reg.provider.health_check())
            except Exception as exc:  # per-provider isolation
                results.append(
                    HealthCheckResult(
                        name=reg.name,
                        status=HealthStatus.DOWN,
                        detail=type(exc).__name__,
                    )
                )
        return results

    async def _run_with_failover(
        self,
        asset_class: AssetClass,
        method: str,
        call: Callable[[IDataProvider], Awaitable[T]],
        *,
        symbol: str = "",
        capability: CapabilityPredicate | None = None,
        retry_on_empty: bool = False,
    ) -> T:
        candidates = self.candidates_for(asset_class, capability=capability)
        if not candidates:
            if capability is not None:
                raise CapabilityNotSupportedError(
                    f"No provider with required capability for asset_class="
                    f"{asset_class.value} method={method}"
                )
            raise NoProviderAvailableError(
                f"No provider configured for asset_class={asset_class.value}"
            )

        last_exc: BaseException | None = None
        last_empty: T | None = None
        for reg in candidates:
            try:
                result = await call(reg.provider)
            except FatalProviderError as exc:
                last_exc = exc
                logger.warning(
                    "data_provider.registry.fatal_skip",
                    provider=reg.name,
                    method=method,
                    symbol=symbol,
                    error=type(exc).__name__,
                )
                continue
            except (TransientProviderError, TimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "data_provider.registry.failover",
                    provider=reg.name,
                    method=method,
                    symbol=symbol,
                    error=type(exc).__name__,
                )
                continue

            if retry_on_empty and (
                result is None
                or _is_empty_dataframe(result)
                or (isinstance(result, dict) and not result)
            ):
                last_empty = result
                logger.info(
                    "data_provider.registry.empty_result",
                    provider=reg.name,
                    method=method,
                    symbol=symbol,
                )
                continue

            return result

        if last_empty is not None:
            return last_empty

        raise NoProviderAvailableError(
            f"All providers failed for asset_class={asset_class.value} "
            f"method={method}: {type(last_exc).__name__ if last_exc else 'none'}"
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
