"""Hermetic fakes for the MCP adapter unit tests.

The adapters in :mod:`engine.mcp.adapters` are pure async functions of the
shape ``(services, principal, arguments) -> dict``. They only reach the outside
world through the injected :class:`~engine.mcp.adapters.EngineServices`, so the
fakes here let us exercise every code path (including error/edge branches)
without a database, network, or disk.

Note: ``tests/mcp`` previously existed only as stale ``.pyc`` bytecode (its
``.py`` sources had been removed). This conftest + test module restore live,
deterministic coverage for the most-recently-changed adapter code.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.core.cost_model import CostBreakdown, Money
from engine.mcp.adapters import EngineServices
from engine.mcp.auth import AuthPrincipal


class FakeMarketDataProvider:
    """Minimal stand-in for :class:`engine.data.feeds.MarketDataProvider`.

    Records every call so tests can assert the adapter passed arguments
    through unchanged (the "dry-run / no-mutation" contract).
    """

    def __init__(
        self,
        df: pd.DataFrame | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._df = df
        self._exc = exc
        self.calls: list[tuple[str, str, str]] = []

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame | None:
        self.calls.append((symbol, period, interval))
        if self._exc is not None:
            raise self._exc
        return self._df


class FakeCostModel:
    """Stand-in cost model returning a deterministic :class:`CostBreakdown`."""

    def __init__(self, total: float = 5.0, pct: float = 0.01) -> None:
        self._total = total
        self._pct = pct
        self.estimate_total_calls: list[dict[str, Any]] = []
        self.estimate_pct_calls: list[dict[str, Any]] = []

    def estimate_total(
        self,
        *,
        symbol: str,
        quantity: int,
        price: float,
        side: str,
        avg_volume: int,
    ) -> CostBreakdown:
        self.estimate_total_calls.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "side": side,
                "avg_volume": avg_volume,
            }
        )
        return CostBreakdown(commission=Money(amount=self._total))

    def estimate_pct(self, *, symbol: str, price: float, side: str) -> float:
        self.estimate_pct_calls.append(
            {"symbol": symbol, "price": price, "side": side}
        )
        return self._pct


@pytest.fixture
def principal() -> AuthPrincipal:
    return AuthPrincipal.anonymous("viewer")


@pytest.fixture
def cost_model() -> FakeCostModel:
    return FakeCostModel()


@pytest.fixture
def market_data_provider() -> FakeMarketDataProvider:
    return FakeMarketDataProvider()


def make_services(
    *,
    provider: FakeMarketDataProvider | None = None,
    cost_model: FakeCostModel | None = None,
) -> EngineServices:
    """Build an :class:`EngineServices` pinned to the supplied fakes."""
    return EngineServices.for_testing(
        market_data_provider=provider or FakeMarketDataProvider(),
        cost_model=cost_model or FakeCostModel(),
    )


@pytest.fixture
def make_df() -> pd.DataFrame:
    """A small, deterministic OHLCV frame with a DatetimeIndex."""
    idx = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [110.0, 111.0, 112.0],
            "low": [99.0, 100.0, 101.0],
            "close": [105.0, 106.0, 107.0],
            "volume": [1_000.0, 1_500.0, 2_000.0],
        },
        index=idx,
    )
