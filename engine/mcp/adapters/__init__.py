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
from engine.mcp.errors import AuthorizationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from engine.data.feeds import MarketDataProvider
    from engine.mcp.auth import AuthPrincipal
    from engine.plugins.registry import PluginRegistry


# Minimum role that may inspect any portfolio regardless of ownership
# (operator/support override). Kept as a module constant so policy stays
# discoverable and adjustable in one place.
_PORTFOLIO_OVERRIDE_ROLE = "quant_dev"


class _SharedOwnedSentinel:
    """Type of the :data:`SHARED_OWNED` sentinel (see there for rationale)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<SHARED_OWNED>"


#: Sentinel owner value marking a portfolio as system/shared — i.e. readable
#: by any *authenticated* principal and owned by no single user. It is
#: deliberately distinct from ``None`` so that an *absent* entry in
#: :attr:`PortfolioStore._owners` (a portfolio id that was never seeded) is
#: unambiguously distinguishable from an *intentionally shared* one. Without
#: this distinction a missing id would be treated as shared, letting a caller
#: probe for portfolio existence — an enumeration vector.
SHARED_OWNED: _SharedOwnedSentinel = _SharedOwnedSentinel()


class PortfolioStore:
    """In-memory store of :class:`~engine.core.portfolio.Portfolio` objects.

    Wraps the engine's core Portfolio type so the MCP server can expose
    portfolio inspection (status / positions / orders) without coupling to the
    SQLAlchemy portfolio table. A single ``default`` portfolio seeded with
    ``$100,000`` is created on construction.

    Ownership model
    ---------------
    Each portfolio optionally records an owning principal ``user_id``. A
    portfolio seeded without an explicit owner (``owner=None``) is treated as
    system/shared and is readable by any authenticated principal — this
    preserves the legacy behaviour of the in-memory default portfolio. A
    portfolio seeded with an explicit owner is readable only by that owner or
    by a principal whose role meets/exceeds :data:`_PORTFOLIO_OVERRIDE_ROLE`.
    """

    def __init__(self, default_capital: float = 100_000.0) -> None:
        self._portfolios: dict[str, Portfolio] = {}
        # portfolio_id -> owning principal user_id, or SHARED_OWNED for a
        # system/shared portfolio. An *absent* key means the portfolio was
        # never seeded (see :meth:`can_access`).
        self._owners: dict[str, str | _SharedOwnedSentinel] = {}
        self.seed("default", default_capital)

    def seed(
        self,
        portfolio_id: str,
        initial_cash: float,
        *,
        owner: str | None = None,
    ) -> Portfolio:
        """Create (or overwrite) a portfolio and record its owner.

        ``owner`` is the ``user_id`` of the :class:`AuthPrincipal` that owns
        the portfolio; pass ``None`` for a system/shared portfolio readable
        by any authenticated principal (stored as :data:`SHARED_OWNED`).
        """
        portfolio = Portfolio(initial_cash=initial_cash, portfolio_id=None)
        self._portfolios[portfolio_id] = portfolio
        self._owners[portfolio_id] = owner if owner is not None else SHARED_OWNED
        return portfolio

    def get(self, portfolio_id: str = "default") -> Portfolio:
        if portfolio_id not in self._portfolios:
            return self.seed(portfolio_id, 100_000.0)
        return self._portfolios[portfolio_id]

    def find(self, portfolio_id: str) -> Portfolio | None:
        """Strict lookup — returns ``None`` when the portfolio is absent.

        Unlike :meth:`get`, this never auto-creates a portfolio, so callers
        can distinguish "missing" from "empty" and raise a clean error.
        """
        return self._portfolios.get(portfolio_id)

    def owner_of(self, portfolio_id: str) -> str | None:
        """Return the owning principal ``user_id``, or ``None`` if shared/absent.

        A shared/system portfolio is reported as ``None`` to preserve the
        public contract even though internally it is stored as
        :data:`SHARED_OWNED`.
        """
        owner = self._owners.get(portfolio_id)
        return None if owner is SHARED_OWNED else owner

    def can_access(self, principal: AuthPrincipal, portfolio_id: str) -> bool:
        """Return ``True`` if ``principal`` may inspect ``portfolio_id``.

        The checks are deliberately ordered to close enumeration and
        privilege-escalation vectors:

        * A portfolio that does not exist is never accessible. This is the
          *first* check so a missing id can never be confused with a shared
          one (which would otherwise let a caller probe for valid ids).
        * An anonymous (unauthenticated) principal is denied *before* any
          owner comparison — a shared portfolio is readable by any
          *authenticated* principal, never by an anonymous one.
        * A shared portfolio (:data:`SHARED_OWNED`) is readable by any
          authenticated principal.
        * An owned portfolio is readable by its owner, or by a principal
          whose role meets/exceeds :data:`_PORTFOLIO_OVERRIDE_ROLE`.
        """
        # 1. Existence: an absent id is never accessible (enumeration guard).
        if portfolio_id not in self._portfolios:
            return False
        # 2. Anonymous principals never read any portfolio, shared or owned.
        if principal.auth_method == "anonymous":
            return False
        owner = self._owners.get(portfolio_id)
        # 3. Shared/system portfolio: any authenticated principal may read.
        if owner is SHARED_OWNED:
            return True
        # 4. Owned portfolio: the owner or an override-role principal.
        if principal.user_id == owner:
            return True
        return principal.has_role(_PORTFOLIO_OVERRIDE_ROLE)

    def assert_access(self, principal: AuthPrincipal, portfolio_id: str) -> None:
        """Raise :class:`AuthorizationError` unless ``principal`` may read."""
        if not self.can_access(principal, portfolio_id):
            raise AuthorizationError(
                f"Principal {principal.user_id!r} is not authorized to "
                f"access portfolio {portfolio_id!r}",
                data={
                    "portfolio_id": portfolio_id,
                    "principal": principal.user_id,
                    "principal_role": principal.role,
                },
            )

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


__all__ = ["SHARED_OWNED", "EngineServices", "PortfolioStore", "to_jsonable"]
