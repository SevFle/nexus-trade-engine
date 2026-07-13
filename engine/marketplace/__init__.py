"""Strategy marketplace domain package.

Hosts lightweight, persistence-agnostic services that back the marketplace
API surface (``engine.api.routes.marketplace``). The :mod:`ratings` submodule
implements the in-memory ratings store used by the strategy ratings
endpoints.
"""

from __future__ import annotations

from engine.marketplace.ratings import (
    InvalidRatingError,
    RatingAggregate,
    RatingRecord,
    RatingsStore,
    get_ratings_store,
)

__all__ = [
    "InvalidRatingError",
    "RatingAggregate",
    "RatingRecord",
    "RatingsStore",
    "get_ratings_store",
]
