"""
IPaperTradeBroker protocol — contract for paper-trade execution backends.

Extends ExecutionBackend with paper-trade-specific capabilities:
position tracking, P&L computation, order lifecycle management,
pre-trade risk checks, fill statistics, and session persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from engine.core.execution.slippage import SlippageModelType

if TYPE_CHECKING:
    from engine.core.execution.base import FillResult


class OrderRejectReason(StrEnum):
    NOT_CONNECTED = "not_connected"
    MARKET_CONDITIONS = "market_conditions"
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    MAX_POSITION_EXCEEDED = "max_position_exceeded"
    ORDER_RATE_EXCEEDED = "order_rate_exceeded"
    DAILY_LOSS_EXCEEDED = "daily_loss_exceeded"
    SYMBOL_BANNED = "symbol_banned"
    INVALID_ORDER = "invalid_order"
    INSUFFICIENT_FUNDS = "insufficient_funds"


class PaperOrderStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class FillPriority(StrEnum):
    FIFO = "fifo"
    PRO_RATA = "pro_rata"


@dataclass
class PaperTradeFill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    commission: float
    timestamp: str
    slippage_bps: float


@dataclass
class PaperPosition:
    symbol: str
    quantity: int
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    market_value: float

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


@dataclass
class PaperPortfolioSnapshot:
    total_equity: float
    cash: float
    positions: dict[str, PaperPosition]
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    timestamp: str

    @property
    def buying_power(self) -> float:
        return self.cash * 0.5 + self.total_equity * 0.5


@dataclass
class PaperTradeRiskConfig:
    max_position_size: int = 10_000
    max_orders_per_minute: int = 60
    max_daily_loss_pct: float = 0.05
    max_open_positions: int = 50
    banned_symbols: set[str] = None
    allowed_symbols: set[str] | None = None
    max_single_order_value: float = 50_000.0

    def __post_init__(self) -> None:
        if self.banned_symbols is None:
            self.banned_symbols = set()


@dataclass
class PaperTradeBrokerConfig:
    fill_probability: float = 0.95
    partial_fill_enabled: bool = True
    partial_fill_min_ratio: float = 0.5
    latency_ms: float = 50.0
    latency_jitter_ms: float = 20.0
    random_seed: int | None = None
    slippage_model_type: SlippageModelType = SlippageModelType.FIXED_BPS
    slippage_model_kwargs: dict[str, Any] = None
    commission_per_share: float = 0.005
    min_commission: float = 1.0
    fill_priority: FillPriority = FillPriority.FIFO
    refresh_price_from_provider: bool = True
    risk_config: PaperTradeRiskConfig = None

    def __post_init__(self) -> None:
        if self.slippage_model_kwargs is None:
            self.slippage_model_kwargs = {}
        if self.risk_config is None:
            self.risk_config = PaperTradeRiskConfig()


@runtime_checkable
class IPaperTradeBroker(Protocol):
    """Paper trade broker interface.

    Extends ExecutionBackend with position tracking, P&L, risk checks,
    and session management. All methods are async and must not raise
    unhandled exceptions.
    """

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def execute(self, order: Any, market_price: float, costs: Any) -> FillResult: ...

    async def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> FillResult: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def get_positions(self) -> dict[str, PaperPosition]: ...

    async def get_portfolio(self) -> PaperPortfolioSnapshot: ...

    async def get_order_history(
        self, limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    async def get_open_orders(self) -> list[dict[str, Any]]: ...

    async def get_fill_stats(self) -> dict[str, Any]: ...

    def update_market_price(self, symbol: str, price: float) -> None: ...
