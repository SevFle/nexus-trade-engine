"""Paper trading session API routes.

Provides REST endpoints for managing paper trading sessions:
start, stop, list, get status, fetch trades, and fill statistics.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from engine.api.auth.dependency import get_current_user
from engine.core.execution.paper_runner import (
    create_and_start_session,
    get_active_session,
)
from engine.core.execution.session import (
    PaperSessionConfig,
    PaperSessionState,
)
from engine.core.execution.session_store import get_paper_session_store
from engine.db.models import User

logger = structlog.get_logger()

router = APIRouter()


class CreateSessionRequest(BaseModel):
    strategy_name: str
    symbols: list[str]
    initial_capital: float = 100_000.0
    interval_seconds: int = 60
    fill_probability: float = 0.95
    partial_fill_enabled: bool = True
    partial_fill_min_ratio: float = 0.5
    latency_ms: float = 50.0
    latency_jitter_ms: float = 20.0
    slippage_model_type: str = "fixed_bps"
    slippage_model_kwargs: dict[str, Any] = {}
    refresh_price_from_provider: bool = True
    random_seed: int | None = None
    strategy_params: dict[str, Any] = {}
    cost_config: dict[str, Any] = {}


class SessionResponse(BaseModel):
    session_id: str
    status: str
    strategy_name: str
    symbols: list[str]
    initial_capital: float
    interval_seconds: int
    created_at: str
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None
    total_trades: int = 0
    total_fills: int = 0
    total_rejections: int = 0
    total_notional: float = 0.0


class TradeResponse(BaseModel):
    trades: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class FillStatsResponse(BaseModel):
    stats: dict[str, Any]


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int


def _state_to_response(state: PaperSessionState) -> SessionResponse:
    return SessionResponse(
        session_id=state.session_id,
        status=state.status.value,
        strategy_name=state.config.strategy_name,
        symbols=state.config.symbols,
        initial_capital=state.config.initial_capital,
        interval_seconds=state.config.interval_seconds,
        created_at=state.created_at,
        started_at=state.started_at,
        stopped_at=state.stopped_at,
        error=state.error,
        total_trades=state.total_trades,
        total_fills=state.total_fills,
        total_rejections=state.total_rejections,
        total_notional=round(state.total_notional, 2),
    )


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def start_session(
    request: CreateSessionRequest,
    user: User = Depends(get_current_user),
) -> SessionResponse:
    from engine.core.execution.slippage import SlippageModelType
    from engine.data.feeds import get_data_provider
    from engine.plugins.registry import PluginRegistry

    try:
        slippage_type = SlippageModelType(request.slippage_model_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid slippage_model_type: {request.slippage_model_type}",
        )

    config = PaperSessionConfig(
        strategy_name=request.strategy_name,
        symbols=request.symbols,
        initial_capital=request.initial_capital,
        interval_seconds=request.interval_seconds,
        fill_probability=request.fill_probability,
        partial_fill_enabled=request.partial_fill_enabled,
        partial_fill_min_ratio=request.partial_fill_min_ratio,
        latency_ms=request.latency_ms,
        latency_jitter_ms=request.latency_jitter_ms,
        slippage_model_type=slippage_type,
        slippage_model_kwargs=request.slippage_model_kwargs,
        refresh_price_from_provider=request.refresh_price_from_provider,
        random_seed=request.random_seed,
        strategy_params=request.strategy_params,
        cost_config=request.cost_config,
    )

    registry = PluginRegistry()
    strategy = registry.load_strategy(request.strategy_name)
    if strategy is None:
        raise HTTPException(
            status_code=404,
            detail=f"Strategy not found: {request.strategy_name}",
        )

    provider = get_data_provider("yahoo")
    store = await get_paper_session_store()

    try:
        runner = await create_and_start_session(
            user_id=str(user.id),
            config=config,
            strategy=strategy,
            provider=provider,
            store=store,
        )
    except Exception as exc:
        logger.exception("paper_api.start_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to start session: {exc}") from exc

    return _state_to_response(runner.session.state)


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    user: User = Depends(get_current_user),
) -> SessionListResponse:
    store = await get_paper_session_store()
    await store.evict_expired()
    raw_sessions = await store.list_by_user(str(user.id))

    responses = []
    for data in raw_sessions:
        resp = SessionResponse(
            session_id=data.get("session_id", ""),
            status=data.get("status", "unknown"),
            strategy_name=data.get("strategy_name", ""),
            symbols=data.get("symbols", []),
            initial_capital=data.get("initial_capital", 0.0),
            interval_seconds=data.get("interval_seconds", 60),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            stopped_at=data.get("stopped_at"),
            error=data.get("error"),
            total_trades=data.get("total_trades", 0),
            total_fills=data.get("total_fills", 0),
            total_rejections=data.get("total_rejections", 0),
            total_notional=data.get("total_notional", 0.0),
        )
        responses.append(resp)

    return SessionListResponse(sessions=responses, total=len(responses))


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    user: User = Depends(get_current_user),
) -> SessionResponse:
    active = get_active_session(session_id)
    if active is not None:
        if active.state.user_id != str(user.id):
            raise HTTPException(status_code=403, detail="Access denied")
        return _state_to_response(active.state)

    store = await get_paper_session_store()
    data = await store.get(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    if data.get("user_id") != str(user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    return SessionResponse(
        session_id=data.get("session_id", ""),
        status=data.get("status", "unknown"),
        strategy_name=data.get("strategy_name", ""),
        symbols=data.get("symbols", []),
        initial_capital=data.get("initial_capital", 0.0),
        interval_seconds=data.get("interval_seconds", 60),
        created_at=data.get("created_at", ""),
        started_at=data.get("started_at"),
        stopped_at=data.get("stopped_at"),
        error=data.get("error"),
        total_trades=data.get("total_trades", 0),
        total_fills=data.get("total_fills", 0),
        total_rejections=data.get("total_rejections", 0),
        total_notional=data.get("total_notional", 0.0),
    )


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def stop_session(
    session_id: str,
    user: User = Depends(get_current_user),
) -> SessionResponse:
    from engine.core.execution.paper_runner import _ACTIVE_TASKS

    active = get_active_session(session_id)
    if active is None:
        raise HTTPException(status_code=404, detail=f"Active session {session_id} not found")

    if active.state.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    task = _ACTIVE_TASKS.get(session_id)
    runner = None

    if task is not None and not task.done():
        runner = getattr(task, "_paper_runner", None)

    if runner is None:
        from engine.core.execution.paper_runner import PaperTradeRunner

        runner = PaperTradeRunner(
            session=active,
            strategy=active.strategy,
            provider=None,  # type: ignore[arg-type]
        )

    await runner.stop()
    return _state_to_response(active.state)


@router.get("/sessions/{session_id}/stats", response_model=FillStatsResponse)
async def get_session_stats(
    session_id: str,
    user: User = Depends(get_current_user),
) -> FillStatsResponse:
    active = get_active_session(session_id)
    if active is None:
        raise HTTPException(status_code=404, detail=f"Active session {session_id} not found")

    if active.state.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    return FillStatsResponse(stats=active.get_fill_stats())


@router.get("/sessions/{session_id}/trades", response_model=TradeResponse)
async def get_session_trades(
    session_id: str,
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(get_current_user),
) -> TradeResponse:
    active = get_active_session(session_id)
    if active is None:
        raise HTTPException(status_code=404, detail=f"Active session {session_id} not found")

    if active.state.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    trades = active.get_trades(limit=limit, offset=offset)
    return TradeResponse(
        trades=trades,
        total=len(active.get_trades(limit=999999)),
        limit=limit,
        offset=offset,
    )


@router.get("/sessions/{session_id}/equity", response_model=list[dict[str, Any]])
async def get_session_equity(
    session_id: str,
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    active = get_active_session(session_id)
    if active is None:
        raise HTTPException(status_code=404, detail=f"Active session {session_id} not found")

    if active.state.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    return active.get_equity_curve()
