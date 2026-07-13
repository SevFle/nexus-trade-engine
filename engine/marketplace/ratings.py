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

import logging
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

_logger = logging.getLogger(__name__)

MAX_REVIEW_LENGTH: int = 2000

STAR_VALUES: tuple[int, ...] = (1, 2, 3, 4, 5)


class InvalidRatingError(ValueError):
    """Raised when a rating submission fails validation.

    Carries a human-readable ``detail`` suitable for surfacing directly in an
    HTTP error response.
    """


def _is_test_environment() -> bool:
    """Return ``True`` when the current process is running under pytest.

    The in-memory store is deliberately loud about being instantiated in a
    production-shaped process: ratings are **not** persisted and would vanish
    on the next process restart. Tests, however, instantiate the store
    freely, so we stay quiet there.
    """
    return "pytest" in sys.modules


def _validate_stars(stars: int) -> int:
    # Reject bools explicitly — ``isinstance(True, int)`` is True in Python,
    # but a boolean star rating is never meaningful and would silently score
    # as 1 (False) / 2 (True).
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


@dataclass
class RatingRecord:
    """A single user's rating (+ optional review) for a strategy."""

    strategy_id: str
    user_id: uuid.UUID
    stars: int
    review: str
    created_at: datetime
    updated_at: datetime


@dataclass
class RatingAggregate:
    """Aggregate rating statistics for a strategy."""

    strategy_id: str
    average: float
    count: int
    # ``str(stars) -> tally`` keyed on the *string* form so the structure
    # serialises cleanly to JSON without bespoke encoding.
    distribution: dict[str, int]


@dataclass
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
    ) -> RatingRecord:
        """Upsert a ``(strategy_id, user_id)`` rating, returning the record."""
        ...

    def get_aggregate(self, strategy_id: str) -> RatingAggregate:
        """Aggregate stats for every rating recorded against ``strategy_id``."""
        ...

    def list_reviews(
        self, strategy_id: str, limit: int = 10, offset: int = 0
    ) -> ReviewPage:
        """A page of reviews (records carrying non-empty review text)."""
        ...


class InMemoryRatingsStore:
    """Thread-safe in-memory implementation of :class:`RatingsStore`.

    Ratings are stored as ``strategy_id -> {user_id(str): RatingRecord}``.
    ``user_id`` is stringified for the inner dict key so the store does not
    require hashable UUID semantics beyond equality.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ratings: dict[str, dict[str, RatingRecord]] = {}
        # The store is a stand-in for a real persistence backend. Surface a
        # loud runtime warning whenever it is spun up outside of the test
        # suite so an operator wiring this into a real process is not
        # silently relying on volatile state.
        if not _is_test_environment():
            _logger.warning(
                "Instantiating InMemoryRatingsStore outside of the test "
                "suite; marketplace ratings are NOT persisted and will be "
                "lost when the process restarts. Configure a persistent "
                "RatingsStore backend for production use."
            )

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
                # Upsert: preserve the original creation timestamp, refresh
                # everything else so a resubmission reflects the latest data.
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
        average = total / count if count else 0.0
        # Keep two decimals so the API surface is stable and human-readable.
        average = round(average, 2)
        return RatingAggregate(
            strategy_id=strategy_id,
            average=average,
            count=count,
            distribution=distribution,
        )

    def list_reviews(
        self, strategy_id: str, limit: int = 10, offset: int = 0
    ) -> ReviewPage:
        if limit < 0:
            raise InvalidRatingError("limit must be >= 0")
        if offset < 0:
            raise InvalidRatingError("offset must be >= 0")

        with self._lock:
            bucket = self._ratings.get(strategy_id, {})
            # Only records that actually carry non-empty review text count as
            # "reviews"; a pure star rating with no text is intentionally
            # excluded from the public review listing.
            reviews = [
                rec
                for rec in bucket.values()
                if rec.review and rec.review.strip()
            ]

        # Most-recently-updated first; user_id is a deterministic tiebreaker
        # so two records that share an `updated_at` never flip order between
        # calls (avoids flaky pagination).
        reviews.sort(key=lambda r: (r.updated_at, str(r.user_id)), reverse=True)

        total = len(reviews)
        # ``limit == 0`` is interpreted as "return no rows" (still report the
        # full match count) rather than "return everything".
        page = reviews[offset : offset + limit] if limit else []
        return ReviewPage(
            reviews=page,
            total=total,
            limit=limit,
            offset=offset,
        )

    def reset(self) -> None:
        """Drop every recorded rating. Used to isolate tests from each other."""
        with self._lock:
            self._ratings.clear()


# Process-singleton state. A plain dict (rather than a module global) is used
# so :func:`reset_default_store` can mutate it in place and any holder of the
# previously-returned store sees the reset reflected through the shared lock.
_default_state: dict[str, InMemoryRatingsStore | None] = {"store": None}
_default_lock = threading.Lock()


def get_ratings_store() -> InMemoryRatingsStore:
    """Return the process-wide default :class:`InMemoryRatingsStore`.

    Lazily instantiated and memoised under :data:`_default_lock` so concurrent
    request handlers share a single store.
    """
    with _default_lock:
        if _default_state["store"] is None:
            _default_state["store"] = InMemoryRatingsStore()
        return _default_state["store"]


def reset_default_store() -> None:
    """Clear the default store, isolating the caller from prior submissions."""
    get_ratings_store().reset()
