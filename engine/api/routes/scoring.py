"""Scoring API routes — run scoring strategies, retrieve results."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.dependency import get_current_user
from engine.db.models import ScoringSnapshot, User
from engine.deps import get_db
from engine.legal.dependencies import require_legal_acceptance
from engine.plugins.registry import PluginRegistry, is_scoring_strategy
from engine.plugins.scoring_executor import ScoringExecutor

router = APIRouter(dependencies=[Depends(require_legal_acceptance)])


class ScoringRunRequest(BaseModel):
    universe: list[str] = Field(..., min_length=1)
    raw_data: dict[str, dict[str, float | None]] = Field(default_factory=dict)


class ScoringRunResponse(BaseModel):
    strategy_id: str
    scores: list[dict[str, Any]]
    excluded_factors: list[str]
    universe_size: int


_STRATEGIES_DIR = None


def _get_registry() -> PluginRegistry:
    return PluginRegistry(_STRATEGIES_DIR)


@router.post("/{strategy_name}/run", response_model=ScoringRunResponse)
async def run_scoring(
    strategy_name: str,
    body: ScoringRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    registry = _get_registry()
    instance = registry.load_strategy(strategy_name)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_name}' not found")

    if not is_scoring_strategy(instance):
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{strategy_name}' is not a scoring strategy",
        )

    executor = ScoringExecutor(instance)
    result = executor.compute_scores(body.universe, body.raw_data)

    snapshot = ScoringSnapshot(
        strategy_id=result.strategy_id,
        universe_size=len(result.scores),
        excluded_factors=result.excluded_factors,
        results=result.to_dict(),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)

    return ScoringRunResponse(
        strategy_id=result.strategy_id,
        scores=[s.to_dict() for s in result.scores],
        excluded_factors=result.excluded_factors,
        universe_size=len(result.scores),
    )


@router.get("/{strategy_name}/results")
async def get_scoring_results(
    strategy_name: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
):
    stmt = (
        select(ScoringSnapshot)
        .where(ScoringSnapshot.strategy_id == strategy_name)
        .order_by(
            ScoringSnapshot.created_at.desc()
            if sort_order == "desc"
            else ScoringSnapshot.created_at.asc()
        )
        .limit(limit)
        .offset(offset)
    )
    snapshots = (await db.execute(stmt)).scalars().all()

    return {
        "strategy_id": strategy_name,
        "results": [
            {
                "id": str(s.id),
                "universe_size": s.universe_size,
                "excluded_factors": s.excluded_factors,
                "scores": s.results.get("scores", []),
                "created_at": s.created_at.isoformat(),
            }
            for s in snapshots
        ],
        "count": len(snapshots),
    }
