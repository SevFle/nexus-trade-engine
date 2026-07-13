"""Lightweight in-memory ratings store for marketplace strategies.

This module backs the ``/api/marketplace/strategies/{strategy_id}/ratings``
endpoints. It is intentionally persistence-agnostic: a process-local
in-memory store is the default implementation (matching the stubbed-out
nature of the rest of the marketplace routes), but the public surface is
defined in terms of a small :class:`RatingsStore` protocol so it can later be
swapped for a SQLAlchemy-backed implementation without touching the route
layer.

Semantics
---------
* One rating per ``(strategy_id, user_id)`` pair — resubmitting updates the
  existing rating (upsert). This mirrors app-store rating conventions and
  keeps a single user from skewing an aggregate.
* ``stars`` must be an integer in ``[1, 5]``; anything else raises
  :class:`InvalidRatingError`.
* ``review`` is optional free text, capped at :data:`MAX_REVIEW_LENGTH`
  characters.
* Aggregate stats cover *every* submitted rating (with or without review
  text), while ``list_reviews`` only returns records that carry non-empty
  review text.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

#: Maximum permitted length (in characters) of a review's free text.
MAX_REVIEW_LENGTH: int = 2_000

#: Valid star values, ascending.
STAR_VALUES: tuple[int, ...] = (1, 2, 3, 4, 5)


class InvalidRatingError(ValueError):
    """Raised when a rating submission fails validation.

    Carries a human-readable ``detail`` suitable for surfacing directly in an
    HTTP error response.
    """


def _validate_stars(stars: int) -> int:
    """Return ``stars`` if it is a valid 1-5 integer, else raise.

    Booleans are rejected explicitly even though ``bool`` is a subclass of
    ``int`` — ``True``/``False`` are nonsensical star ratings.
    """
    if isinstance(stars, bool) or not isinstance(stars, int):
        raise InvalidRatingError("stars must be an integer")
    if stars not in STAR_VALUES:
        raise InvalidRatingError("stars must be an integer between 1 and 5")
    return stars


def _validate_review(review: str) -> str:
    if review is None:
        return ""
    if not isinstance(review, str):
        raise InvalidRatingError("review must be a string")
    if len(review) > MAX_REVIEW_LENGTH:
        raise InvalidRatingError(
            f"review must be at most {MAX_REVIEW_LENGTH} characters"
        )
    return review


@dataclass(frozen=True)
class RatingRecord:
    """A single user's rating (+ optional review) for a strategy."""

    strategy_id: str
    user_id: uuid.UUID
    stars: int
    review: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RatingAggregate:
    """Aggregate rating statistics for a strategy."""

    strategy_id: str
    average: float
    count: int
    #: Count of ratings per star bucket, keyed by the star value as a string
    #: (``"1"``..``"5"``) so it serialises cleanly to JSON.
    distribution: dict[str, int]


@dataclass(frozen=True)
class ReviewPage:
    """A page of recent reviews plus the total matching-review count."""

    reviews: list[RatingRecord]
    total: int
    limit: int
    offset: int


class RatingsStore(Protocol):
    """Persistence-agnostic ratings store contract."""

    def submit_rating(
        self,
        strategy_id: str,
        user_id: uuid.UUID,
        stars: int,
        review: str = "",
    ) -> RatingRecord: ...

    def get_aggregate(self, strategy_id: str) -> RatingAggregate: ...

    def list_reviews(
        self,
        strategy_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> ReviewPage: ...


class InMemoryRatingsStore:
    """Thread-safe in-memory implementation of :class:`RatingsStore`.

    Ratings are stored as ``strategy_id -> {user_id(str): RatingRecord}``.
    ``user_id`` is stringified for the inner dict key so the store does not
    require hashable UUID semantics beyond equality.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ratings: dict[str, dict[str, RatingRecord]] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def submit_rating(
        self,
        strategy_id: str,
        user_id: uuid.UUID,
        stars: int,
        review: str = "",
    ) -> RatingRecord:
        if not strategy_id:
            raise InvalidRatingError("strategy_id must be a non-empty string")
        if not isinstance(user_id, uuid.UUID):
            raise InvalidRatingError("user_id must be a uuid.UUID")

        stars = _validate_stars(stars)
        review = _validate_review(review)

        now = datetime.now(tz=UTC)
        key = str(user_id)
        with self._lock:
            bucket = self._ratings.setdefault(strategy_id, {})
            existing = bucket.get(key)
            if existing is None:
                record = RatingRecord(
                    strategy_id=strategy_id,
                    user_id=user_id,
                    stars=stars,
                    review=review,
                    created_at=now,
                    updated_at=now,
                )
            else:
                # Upsert: preserve original creation timestamp, refresh
                # everything else.
                record = RatingRecord(
                    strategy_id=strategy_id,
                    user_id=user_id,
                    stars=stars,
                    review=review,
                    created_at=existing.created_at,
                    updated_at=now,
                )
            bucket[key] = record
            return record

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_aggregate(self, strategy_id: str) -> RatingAggregate:
        with self._lock:
            bucket = self._ratings.get(strategy_id, {})
            records = list(bucket.values())

        distribution = {str(s): 0 for s in STAR_VALUES}
        total = 0
        for rec in records:
            distribution[str(rec.stars)] += 1
            total += rec.stars

        count = len(records)
        average = (total / count) if count else 0.0
        # Round to one decimal place to avoid float noise in the API payload
        # while preserving enough precision to be meaningful.
        average = round(average, 2)
        return RatingAggregate(
            strategy_id=strategy_id,
            average=average,
            count=count,
            distribution=distribution,
        )

    def list_reviews(
        self,
        strategy_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> ReviewPage:
        if limit < 0:
            raise InvalidRatingError("limit must be >= 0")
        if offset < 0:
            raise InvalidRatingError("offset must be >= 0")

        with self._lock:
            bucket = self._ratings.get(strategy_id, {})
            # Only records that carry non-empty review text count as reviews.
            reviews = [
                rec for rec in bucket.values() if rec.review and rec.review.strip()
            ]
            # Most recent first; ties broken by user_id for deterministic
            # ordering in tests.
            reviews.sort(
                key=lambda r: (r.updated_at, str(r.user_id)),
                reverse=True,
            )
            total = len(reviews)
            page = reviews[offset : offset + limit] if limit else []

        return ReviewPage(reviews=page, total=total, limit=limit, offset=offset)

    # ------------------------------------------------------------------
    # Test/maintenance helpers
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Drop all stored ratings. Intended for test isolation."""
        with self._lock:
            self._ratings.clear()


# ---------------------------------------------------------------------------
# Process-wide default instance.
#
# The route layer resolves the store through :func:`get_ratings_store` so
# tests can call :meth:`InMemoryRatingsStore.reset` on the singleton to keep
# test cases isolated.
# ---------------------------------------------------------------------------

# Mutable container holding the lazily-created singleton. Wrapping the
# reference in a dict lets us mutate it from within ``get_ratings_store``
# without resorting to a ``global`` declaration.
_default_state: dict[str, InMemoryRatingsStore | None] = {"store": None}
_default_lock = threading.Lock()


def get_ratings_store() -> InMemoryRatingsStore:
    """Return the process-wide :class:`InMemoryRatingsStore` singleton."""
    with _default_lock:
        if _default_state["store"] is None:
            _default_state["store"] = InMemoryRatingsStore()
        return _default_state["store"]


def reset_default_store() -> None:
    """Reset the singleton store — convenience wrapper for tests."""
    get_ratings_store().reset()
