"""
Marketplace API — browse, search, install, and rate community strategies.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from engine.api.auth.dependency import get_current_user, require_role
from engine.db.models import User
from engine.legal.dependencies import require_legal_acceptance
from engine.marketplace.ratings import (
    InvalidRatingError,
    RatingAggregate,
    RatingRecord,
    RatingsStore,
    get_ratings_store,
)

router = APIRouter(dependencies=[Depends(require_legal_acceptance)])


class MarketplaceEntry(BaseModel):
    id: str
    name: str
    version: str
    author: str
    description: str
    category: str
    tags: list[str] = []
    rating: float = 0.0
    downloads: int = 0
    backtest_sharpe: float | None = None
    min_capital: float = 0.0


class InstallRequest(BaseModel):
    strategy_id: str
    version: str = "latest"


class RatingRequest(BaseModel):
    """Body for ``POST /strategies/{strategy_id}/ratings``."""

    stars: int = Field(..., ge=1, le=5, description="Star rating, 1-5 inclusive.")
    review: str | None = Field(
        None, max_length=2000, description="Optional free-text review (<= 2000 chars)."
    )


class RatingResponse(BaseModel):
    """A stored rating record, as returned to the submitting user."""

    strategy_id: str
    user_id: str
    stars: int
    review: str
    created_at: datetime
    updated_at: datetime


class AggregateResponse(BaseModel):
    """Aggregate rating statistics for a strategy."""

    strategy_id: str
    average: float
    count: int
    distribution: dict[str, int]


class ReviewItem(BaseModel):
    """A single review in a public reviews listing."""

    user_id: str
    stars: int
    review: str
    updated_at: datetime


class RatingsListResponse(BaseModel):
    """Response body for ``GET /strategies/{strategy_id}/ratings``."""

    strategy_id: str
    aggregate: AggregateResponse
    reviews: list[ReviewItem]
    total: int
    limit: int
    offset: int


def _record_to_response(record: RatingRecord) -> RatingResponse:
    return RatingResponse(
        strategy_id=record.strategy_id,
        user_id=str(record.user_id),
        stars=record.stars,
        review=record.review,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _aggregate_to_response(agg: RatingAggregate) -> AggregateResponse:
    return AggregateResponse(
        strategy_id=agg.strategy_id,
        average=agg.average,
        count=agg.count,
        distribution=dict(agg.distribution),
    )


@router.get("/browse")
async def browse_marketplace(
    category: str | None = None,
    search: str | None = None,
    sort_by: str = "downloads",
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(get_current_user),
):
    """Browse available strategies in the marketplace."""
    # TODO: Query marketplace registry (could be remote API or local DB)
    return {
        "strategies": [],
        "total": 0,
        "page": page,
        "per_page": per_page,
        "filters": {"category": category, "search": search, "sort_by": sort_by},
    }


@router.get("/categories")
async def list_categories(user: User = Depends(get_current_user)):
    """List available strategy categories."""
    return {
        "categories": [
            {
                "id": "algorithmic",
                "name": "Fixed Algorithm",
                "description": "Deterministic rule-based strategies",
            },
            {
                "id": "ml",
                "name": "Machine Learning",
                "description": "Neural nets, ensemble models, deep learning",
            },
            {
                "id": "llm",
                "name": "LLM-Powered",
                "description": "Strategies using large language models",
            },
            {
                "id": "hybrid",
                "name": "Hybrid / Multi-Model",
                "description": "Combinations of multiple approaches",
            },
            {
                "id": "income",
                "name": "Income / Yield",
                "description": "Dividend and options income strategies",
            },
            {
                "id": "macro",
                "name": "Macro / Regime",
                "description": "Macro-driven allocation strategies",
            },
        ]
    }


@router.post("/install")
async def install_strategy(
    req: InstallRequest,
    user: User = Depends(require_role("developer")),
):
    """Install a strategy from the marketplace."""
    # TODO: Download strategy package, validate manifest, install to plugin dir
    return {
        "status": "not_implemented",
        "strategy_id": req.strategy_id,
        "message": "Marketplace installation coming soon.",
    }


@router.delete("/uninstall/{strategy_id}")
async def uninstall_strategy(
    strategy_id: str,
    user: User = Depends(require_role("developer")),
):
    """Uninstall a strategy."""
    # TODO: Deactivate, remove files, update DB
    return {"status": "not_implemented", "strategy_id": strategy_id}


@router.post("/{strategy_id}/rate")
async def rate_strategy(
    strategy_id: str,
    rating: int,
    review: str = "",
    user: User = Depends(get_current_user),
):
    """Rate and review a marketplace strategy (legacy stub).

    Kept for backwards compatibility with existing clients that POST to the
    ``/{strategy_id}/rate`` path with ``rating`` as a query parameter. This
    path is distinct from the newer resource-oriented ``/strategies/{id}/``
    "ratings`` endpoint below and does not shadow it.
    """
    if not 1 <= rating <= 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")
    return {"status": "not_implemented"}


@router.post(
    "/strategies/{strategy_id}/ratings",
    response_model=RatingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_rating(
    strategy_id: str,
    payload: RatingRequest,
    user: User = Depends(get_current_user),
    store: RatingsStore = Depends(get_ratings_store),
):
    """Submit (or update) the authenticated user's rating for a strategy.

    One rating per ``(strategy_id, user_id)``: resubmitting updates the
    existing record in place (upsert) and returns the new state. Star value
    is clamped to ``[1, 5]`` at the schema layer; deeper validation errors
    surface as HTTP 400.
    """
    try:
        record = store.submit_rating(
            strategy_id,
            user.id,
            payload.stars,
            payload.review,
        )
    except InvalidRatingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _record_to_response(record)


@router.get(
    "/strategies/{strategy_id}/ratings",
    response_model=RatingsListResponse,
)
async def get_ratings(
    strategy_id: str,
    limit: int = Query(10, ge=0, le=100, description="Max reviews to return."),
    offset: int = Query(0, ge=0, description="Reviews to skip."),
    user: User = Depends(get_current_user),
    store: RatingsStore = Depends(get_ratings_store),
):
    """Fetch aggregate rating stats plus a page of public reviews.

    The aggregate covers every submitted rating (with or without review
    text); the ``reviews`` list only includes records carrying non-empty
    review text, most-recently-updated first.
    """
    try:
        aggregate = store.get_aggregate(strategy_id)
        page = store.list_reviews(strategy_id, limit=limit, offset=offset)
    except InvalidRatingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return RatingsListResponse(
        strategy_id=strategy_id,
        aggregate=_aggregate_to_response(aggregate),
        reviews=[
            ReviewItem(
                user_id=str(rec.user_id),
                stars=rec.stars,
                review=rec.review,
                updated_at=rec.updated_at,
            )
            for rec in page.reviews
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
