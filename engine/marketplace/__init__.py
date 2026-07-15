"""Strategy marketplace domain package.

Hosts lightweight, persistence-agnostic services that back the marketplace
API surface (``engine.api.routes.marketplace``). Submodules:

* :mod:`ratings` — the in-memory ratings store behind the strategy ratings
  endpoints.
* :mod:`search` — the keyword search & filtering service behind the
  ``GET /api/marketplace/search`` endpoint.
"""

from __future__ import annotations

from engine.marketplace.ratings import (
    InvalidRatingError,
    RatingAggregate,
    RatingRecord,
    RatingsStore,
    get_ratings_store,
)
from engine.marketplace.search import (
    ALLOWED_SORTS,
    DEFAULT_LIMIT,
    DEFAULT_SORT,
    EMPTY_QUERY_FALLBACK_SORT,
    InMemoryStrategyCatalog,
    SearchError,
    SearchHit,
    SearchPage,
    StrategyCatalog,
    StrategyListing,
    get_strategy_catalog,
    reset_default_catalog,
)

__all__ = [
    "ALLOWED_SORTS",
    "DEFAULT_LIMIT",
    "DEFAULT_SORT",
    "EMPTY_QUERY_FALLBACK_SORT",
    "InMemoryStrategyCatalog",
    "InvalidRatingError",
    "RatingAggregate",
    "RatingRecord",
    "RatingsStore",
    "SearchError",
    "SearchHit",
    "SearchPage",
    "StrategyCatalog",
    "StrategyListing",
    "get_ratings_store",
    "get_strategy_catalog",
    "reset_default_catalog",
]
