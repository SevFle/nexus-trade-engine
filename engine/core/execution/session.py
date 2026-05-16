"""
Paper trading session management.

Manages the lifecycle of paper trading sessions: configuration,
state tracking, and in-memory session registry.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.execution.clock import SystemClock
from engine.core.execution.paper import PaperBackend, PaperTradeConfig
from engine.core.execution.paper_broker_interface import PaperTradeBrokerConfig
from engine.core.execution.paper_trade_backend import PaperTradeExecutionBackend
from engine.core.execution.slippage import SlippageModelType

if TYPE_CHECKING:
    from engine.core.execution.base import ExecutionBackend

logger = structlog.get_logger()


class SessionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class PaperSessionConfig:
    strategy_name: str
    symbols: list[str]
    initial_capital: float = 100_000.0
    interval_seconds: int = 60
    fill_probability: float = 0.95
    partial_fill_enabled: bool = True
    partial_fill_min_ratio: float = 0.5
    latency_ms: float = 50.0
    latency_jitter_ms: float = 20.0
    slippage_model_type: SlippageModelType = SlippageModelType.FIXED_BPS
    slippage_model_kwargs: dict[str, Any] = field(default_factory=dict)
    refresh_price_from_provider: bool = True
    random_seed: int | None = None
    strategy_params: dict[str, Any] = field(default_factory=dict)
    cost_config: dict[str, Any] = field(default_factory=dict)
    max_bars_history: int = 50


@dataclass
class PaperSessionState:
    session_id: str
    user_id: str
    config: PaperSessionConfig
    status: SessionStatus = SessionStatus.PENDING
    created_at: str = ""
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None

    total_trades: int = 0
    total_fills: int = 0
    total_rejections: int = 0
    total_notional: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "status": self.status.value,
            "strategy_name": self.config.strategy_name,
            "symbols": self.config.symbols,
            "initial_capital": self.config.initial_capital,
            "interval_seconds": self.config.interval_seconds,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "error": self.error,
            "total_trades": self.total_trades,
            "total_fills": self.total_fills,
            "total_rejections": self.total_rejections,
            "total_notional": round(self.total_notional, 2),
        }


class PaperTradeSession:
    """
    Manages a single paper trading session's runtime components.

    Owns the PaperBackend, Portfolio, OrderManager, and other components
    needed to run a paper trade. The PaperTradeRunner drives the evaluation
    loop; this class holds the mutable state.
    """

    def __init__(
        self,
        state: PaperSessionState,
        data_provider: Any = None,
    ) -> None:
        self.state = state
        self._data_provider = data_provider
        self._backend: ExecutionBackend | None = None
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._portfolio: Any = None
        self._order_manager: Any = None
        self._strategy: Any = None
        self._equity_curve: list[dict[str, Any]] = []
        self._trades: list[dict[str, Any]] = []

    def create_backend(
        self,
        *,
        use_full_backend: bool = False,
        event_bus: Any = None,
        metrics: Any = None,
    ) -> ExecutionBackend:
        config = self.state.config

        if use_full_backend:
            broker_config = PaperTradeBrokerConfig(
                fill_probability=config.fill_probability,
                partial_fill_enabled=config.partial_fill_enabled,
                partial_fill_min_ratio=config.partial_fill_min_ratio,
                latency_ms=config.latency_ms,
                latency_jitter_ms=config.latency_jitter_ms,
                slippage_model_type=config.slippage_model_type,
                slippage_model_kwargs=config.slippage_model_kwargs,
                refresh_price_from_provider=config.refresh_price_from_provider,
                random_seed=config.random_seed,
            )
            self._backend = PaperTradeExecutionBackend(
                config=broker_config,
                initial_cash=config.initial_capital,
                data_provider=self._data_provider,
                event_bus=event_bus,
                clock=SystemClock(),
                metrics=metrics,
            )
            return self._backend

        paper_config = PaperTradeConfig(
            fill_probability=config.fill_probability,
            partial_fill_enabled=config.partial_fill_enabled,
            partial_fill_min_ratio=config.partial_fill_min_ratio,
            latency_ms=config.latency_ms,
            latency_jitter_ms=config.latency_jitter_ms,
            slippage_model_type=config.slippage_model_type,
            slippage_model_kwargs=config.slippage_model_kwargs,
            refresh_price_from_provider=config.refresh_price_from_provider,
            random_seed=config.random_seed,
        )
        self._backend = PaperBackend(config=paper_config, data_provider=self._data_provider)
        return self._backend

    @property
    def backend(self) -> ExecutionBackend | None:
        return self._backend

    @property
    def portfolio(self) -> Any:
        return self._portfolio

    @portfolio.setter
    def portfolio(self, value: Any) -> None:
        self._portfolio = value

    @property
    def order_manager(self) -> Any:
        return self._order_manager

    @order_manager.setter
    def order_manager(self, value: Any) -> None:
        self._order_manager = value

    @property
    def strategy(self) -> Any:
        return self._strategy

    @strategy.setter
    def strategy(self, value: Any) -> None:
        self._strategy = value

    def record_trade(self, trade: dict[str, Any]) -> None:
        self._trades.append(trade)
        self.state.total_trades += 1
        if trade.get("status") == "filled":
            self.state.total_fills += 1
            self.state.total_notional += trade.get("quantity", 0) * trade.get("fill_price", 0)
        else:
            self.state.total_rejections += 1

    def record_equity(self, point: dict[str, Any]) -> None:
        self._equity_curve.append(point)

    def get_trades(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self._trades[offset : offset + limit]

    def get_equity_curve(self) -> list[dict[str, Any]]:
        return list(self._equity_curve)

    def get_fill_stats(self) -> dict[str, Any]:
        if self._backend is None:
            return {}
        if isinstance(self._backend, PaperTradeExecutionBackend):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                task = loop.create_task(self._backend.get_fill_stats())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return {}
            return {}
        if isinstance(self._backend, PaperBackend):
            global_stats = self._backend.stats.as_dict()
            per_symbol = {
                sym: self._backend.get_symbol_stats(sym).as_dict()
                for sym in self.state.config.symbols
            }
            return {
                "global": global_stats,
                "per_symbol": per_symbol,
            }
        return {}

    def mark_started(self) -> None:
        self.state.status = SessionStatus.RUNNING
        self.state.started_at = datetime.now(UTC).isoformat()

    def mark_stopped(self, error: str | None = None) -> None:
        if error:
            self.state.status = SessionStatus.FAILED
            self.state.error = error
        else:
            self.state.status = SessionStatus.STOPPED
        self.state.stopped_at = datetime.now(UTC).isoformat()


def create_session_id() -> str:
    return str(uuid.uuid4())
