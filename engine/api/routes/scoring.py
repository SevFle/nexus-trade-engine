"""Scoring API routes — run scoring strategies, retrieve results."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.dependency import get_current_user
from engine.api.validators import SafeIdentifier
from engine.db.models import ScoringSnapshot, User
from engine.deps import get_db
from engine.legal.dependencies import require_legal_acceptance
from engine.legal.scoring_gate import (
    LegalScoreValidator,
    get_default_score_validator,
)
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


# Strategy identifiers are validated at the FastAPI layer via the shared
# :data:`engine.api.validators.SafeIdentifier` alias (pattern + max length).
# A malformed or hostile identifier (markup, path traversal, control chars)
# is rejected with a 422 *before* the handler runs, so it can never reach
# a registry lookup, DB query, log line, or reflected error detail.

_STRATEGIES_DIR = None


def _get_registry() -> PluginRegistry:
    return PluginRegistry(_STRATEGIES_DIR)


def get_score_validator() -> LegalScoreValidator:
    """FastAPI dependency: the process-wide legal score validator.

    Scoring surfaces call this before returning so that flagged strategies'
    scores are suppressed and over-ceiling scores are capped. Exposed as a
    dependency (rather than imported inline) so tests can override it with a
    controlled :class:`LegalScoreValidator` via ``app.dependency_overrides``.
    """
    return get_default_score_validator()


def _gate_score_dicts(
    strategy_id: str,
    scores: list[dict[str, Any]],
    validator: LegalScoreValidator,
) -> list[dict[str, Any]]:
    """Gate already-serialised scores (from a stored snapshot) for exposure.

    The read path returns scores as plain dicts (JSONB round-trip), so it
    cannot rebuild a :class:`nexus_sdk.scoring.ScoringResult`. Instead it
    validates each entry in place: suppressed entries are dropped and
    capped composites are clamped. Stored ``rank`` values are preserved for
    survivors so historical ordering stays stable.
    """
    gated: list[dict[str, Any]] = []
    for entry in scores:
        outcome = validator.validate_score(strategy_id, entry.get("composite_score"))
        if outcome.suppressed:
            continue
        if outcome.capped:
            gated_entry = dict(entry)
            gated_entry["composite_score"] = outcome.score
            gated.append(gated_entry)
        else:
            gated.append(entry)
    return gated


@router.post("/{strategy_name}/run", response_model=ScoringRunResponse)
async def run_scoring(
    strategy_name: SafeIdentifier,
    body: ScoringRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    validator: LegalScoreValidator = Depends(get_score_validator),
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

    # Legal compliance gate: suppress flagged-strategy scores and cap
    # over-ceiling composites BEFORE the result is persisted or returned.
    # Gating at the compute→expose boundary means the persisted snapshot and
    # every downstream surface see the same compliant view.
    result = validator.validate_result(result)

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
    strategy_name: SafeIdentifier,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    validator: LegalScoreValidator = Depends(get_score_validator),
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

    # Defense-in-depth: re-apply the legal gate on the read path so that any
    # snapshot persisted before a strategy was flagged (or before the cap was
    # tightened) is brought into compliance at exposure time too.
    return {
        "strategy_id": strategy_name,
        "results": [
            {
                "id": str(s.id),
                "universe_size": s.universe_size,
                "excluded_factors": s.excluded_factors,
                "scores": _gate_score_dicts(strategy_name, s.results.get("scores", []), validator),
                "created_at": s.created_at.isoformat(),
            }
            for s in snapshots
        ],
        "count": len(snapshots),
    }
