"""Adapter layer bridging MCP tool calls to the Nexus engine.

The :class:`EngineServices` container is the single dependency the MCP server
needs. Every adapter is a pure async function
``(services, principal, arguments) -> dict`` so they are trivial to unit-test
in isolation and so the server can compose them without a running database.

Components are *injectable* with sensible default factories that build the
real engine objects (``PluginRegistry``, ``DefaultCostModel``, the configured
market-data provider). This keeps the server usable in two modes:

* **Online** — default factories hit live providers (Yahoo) and read strategy
  manifests from disk.
* **Hermetic** — tests inject fakes (e.g. an in-memory market-data provider
  and a stub registry) so no network or DB is required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.core.cost_model import DefaultCostModel
from engine.core.portfolio import Portfolio

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from engine.data.feeds import MarketDataProvider
    from engine.plugins.registry import PluginRegistry


class PortfolioStore:
    """In-memory store of :class:`~engine.core.portfolio.Portfolio` objects.

    Wraps the engine's core Portfolio type so the MCP server can expose
    portfolio inspection (status / positions / orders) without coupling to the
    SQLAlchemy portfolio table. A single ``default`` portfolio seeded with
    ``$100,000`` is created on construction.
    """

    def __init__(self, default_capital: float = 100_000.0) -> None:
        self._portfolios: dict[str, Portfolio] = {}
        self.seed("default", default_capital)

    def seed(self, portfolio_id: str, initial_cash: float) -> Portfolio:
        portfolio = Portfolio(initial_cash=initial_cash, portfolio_id=None)
        self._portfolios[portfolio_id] = portfolio
        return portfolio

    def get(self, portfolio_id: str = "default") -> Portfolio:
        if portfolio_id not in self._portfolios:
            return self.seed(portfolio_id, 100_000.0)
        return self._portfolios[portfolio_id]

    def list_ids(self) -> list[str]:
        return sorted(self._portfolios)


def _default_registry() -> PluginRegistry:
    from engine.plugins.registry import PluginRegistry

    return PluginRegistry()


def _default_provider_factory() -> Callable[[], MarketDataProvider]:
    def _factory() -> MarketDataProvider:
        from engine.data.feeds import get_data_provider
        from engine.mcp.config import mcp_settings

        return get_data_provider(mcp_settings.backtest_default_provider)

    return _factory


@dataclass
class EngineServices:
    """Container of injectable engine capabilities used by the adapters."""

    plugin_registry: PluginRegistry = field(default_factory=_default_registry)
    portfolio_store: PortfolioStore = field(default_factory=PortfolioStore)
    cost_model: DefaultCostModel = field(default_factory=DefaultCostModel)
    market_data_provider_factory: Callable[[], MarketDataProvider] = field(
        default_factory=_default_provider_factory
    )
    strategies_dir: Path | None = None

    @classmethod
    def for_testing(
        cls,
        *,
        plugin_registry: PluginRegistry | None = None,
        portfolio_store: PortfolioStore | None = None,
        cost_model: DefaultCostModel | None = None,
        market_data_provider: MarketDataProvider | None = None,
        strategies_dir: Path | None = None,
    ) -> EngineServices:
        """Build a services instance pinned to the given (usually fake) parts.

        ``market_data_provider`` is captured once and returned on every call,
        which is what hermetic tests want. Online deployments should construct
        :class:`EngineServices` directly so a fresh provider is built per
        backtest.
        """
        provider = market_data_provider
        factory: Callable[[], MarketDataProvider] = (
            (lambda: provider) if provider is not None else _default_provider_factory()
        )
        # Keep the registry and strategies_dir coherent: if a caller points
        # at a temp strategies directory but does not inject a registry,
        # build one that reads that directory so both the strategy adapters
        # (which consult the registry) and the resources layer (which reads
        # strategies_dir directly) observe the same catalog.
        if plugin_registry is not None:
            registry: PluginRegistry = plugin_registry
        elif strategies_dir is not None:
            from engine.plugins.registry import PluginRegistry as _PluginRegistry

            registry = _PluginRegistry(strategies_dir)
        else:
            registry = _default_registry()
        return cls(
            plugin_registry=registry,
            portfolio_store=portfolio_store or PortfolioStore(),
            cost_model=cost_model or DefaultCostModel(),
            market_data_provider_factory=factory,
            strategies_dir=strategies_dir,
        )


def to_jsonable(obj: Any) -> Any:
    """Best-effort conversion of engine objects to JSON-serialisable forms.

    Handles datetimes (ISO-8601), Decimals, dataclasses, and pandas/numpy
    scalars without pulling those libraries eagerly.
    """
    import datetime as _dt
    from dataclasses import asdict, is_dataclass
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, _dt.datetime | _dt.date | _dt.timedelta):
        return obj.total_seconds() if isinstance(obj, _dt.timedelta) else obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        # Reduce dataclass instances to their dict form so they flow through
        # the generic mapping branch below.
        obj = asdict(obj)  # type: ignore[arg-type]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float | int):
        # Normalise NaN/inf (common from metrics) to None for valid JSON.
        import math

        return None if math.isnan(obj) or math.isinf(obj) else obj
    return obj


__all__ = ["EngineServices", "PortfolioStore", "to_jsonable"]
