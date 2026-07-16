"""Tests for ``SearchResultItem`` null/None passthrough and ``_hit_to_item``.

These tests address a critical review finding that the marketplace search
result serialization had **zero direct test coverage** for how ``None`` values
on a :class:`StrategyListing` flow through ``_hit_to_item`` into the Pydantic
:class:`SearchResultItem`, and how those ``None`` values render in the JSON
response body.

Contract under test
-------------------
The reviewer enumerated a superset of fields — ``backtest_sharpe``,
``created_at``, ``rating``, ``downloads``, ``min_capital``, ``description``.
Of these, ``backtest_sharpe``, ``created_at``, ``rating``, ``downloads`` and
``min_capital`` are *genuinely Optional* (``float | None`` / ``int | None`` /
``datetime | None``) on both the source dataclass (:class:`StrategyListing`)
and the response model (:class:`SearchResultItem`). They default to ``None``,
accept ``None`` without error, and serialize to the JSON literal ``null``.
``_hit_to_item`` only forwards keys whose source value is present (non-None),
so missing catalog data surfaces as the model's ``None`` default rather than
a masked ``0``/``0.0`` placeholder.

The remaining field (``description``) is a *required, non-nullable* ``str`` on
the model. It rejects ``None`` with a ``pydantic.ValidationError``; at the
data layer it is a required ``str``. This suite locks in that contract
precisely so any future relaxation (e.g. making it Optional to tolerate
missing catalog data) is a deliberate, reviewed change rather than a silent
API-shape drift — and so the null-handling that *does* exist is covered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from engine.api.routes.marketplace import SearchResultItem, _hit_to_item
from engine.marketplace.search import SearchHit, StrategyListing

# Fields that are genuinely Optional on SearchResultItem. These are the
# ones where None genuinely flows end-to-end and must render as JSON ``null``.
OPTIONAL_FIELDS: tuple[str, ...] = (
    "backtest_sharpe",
    "created_at",
    "rating",
    "downloads",
    "min_capital",
)

# Fields the review enumerated that are actually *required* (non-nullable) on
# the model. They must NOT silently swallow None.
REQUIRED_FIELDS: tuple[str, ...] = ("description",)


def _minimal_listing(listing_id: str = "sparse-1") -> StrategyListing:
    """A listing with only its required fields populated.

    Every genuinely-Optional field is left at its default (``None``), and the
    non-Optional numeric fields sit at their data-layer defaults. This is the
    most "missing data"-shaped listing that still produces a valid item.
    """
    return StrategyListing(
        id=listing_id,
        name="Sparse Strategy",
        version="0.1.0",
        author="Anonymous",
        description="",
        category="algorithmic",
        tags=[],
        rating=0.0,
        downloads=0,
        backtest_sharpe=None,
        min_capital=0.0,
        created_at=None,
    )


def _full_listing(listing_id: str = "full-1") -> StrategyListing:
    """A listing with every field, including the Optional ones, populated."""
    return StrategyListing(
        id=listing_id,
        name="Full Strategy",
        version="2.3.4",
        author="Nexus Labs",
        description="A fully-specified strategy with all metadata present.",
        category="ml",
        tags=["momentum", "neural"],
        rating=4.7,
        downloads=2048,
        backtest_sharpe=1.53,
        min_capital=25_000.0,
        created_at=datetime(2024, 5, 1, 12, 30, tzinfo=UTC),
    )


def _hit(listing: StrategyListing, score: float = 0.0) -> SearchHit:
    return SearchHit(listing=listing, score=score)


# ---------------------------------------------------------------------------
# (a) None passthrough for genuinely-Optional fields via _hit_to_item
# ---------------------------------------------------------------------------


class TestNonePassthroughOptionalFields:
    """``_hit_to_item`` must carry ``None`` through for the Optional fields."""

    def test_hit_to_item_yields_none_for_optional_fields(self):
        listing = _minimal_listing()
        item = _hit_to_item(_hit(listing, score=0.0))

        # No exception is raised merely by constructing the item (implicit —
        # we reached the asserts). The Optional fields must be exactly None.
        assert item.backtest_sharpe is None
        assert item.created_at is None

    def test_constructing_search_result_item_with_explicit_none(self):
        """Direct model construction with explicit None must not raise."""
        item = SearchResultItem(
            id="x",
            name="X",
            version="1.0.0",
            author="A",
            description="d",
            category="c",
            tags=[],
            rating=0.0,
            downloads=0,
            backtest_sharpe=None,
            min_capital=0.0,
            created_at=None,
        )
        assert item.backtest_sharpe is None
        assert item.created_at is None

    def test_optional_fields_default_to_none_when_omitted(self):
        """Omitting the Optional fields must yield None (not a default value)."""
        item = SearchResultItem(
            id="x",
            name="X",
            version="1.0.0",
            author="A",
            description="d",
            category="c",
            tags=[],
            rating=0.0,
            downloads=0,
            min_capital=0.0,
        )
        assert item.backtest_sharpe is None
        assert item.created_at is None

    @pytest.mark.parametrize("field", OPTIONAL_FIELDS)
    def test_no_validation_error_for_none_on_optional_field(self, field: str):
        """Constructing an item with ``None`` for an Optional field is valid."""
        base = {
            "id": "x",
            "name": "X",
            "version": "1.0.0",
            "author": "A",
            "description": "d",
            "category": "c",
            "tags": [],
            "rating": 0.0,
            "downloads": 0,
            "min_capital": 0.0,
        }
        base[field] = None
        # Must not raise.
        item = SearchResultItem(**base)
        assert getattr(item, field) is None


# ---------------------------------------------------------------------------
# (b) JSON serialization produces `null` for the Optional fields
# ---------------------------------------------------------------------------


class TestJsonNullSerialization:
    """Serialized output must emit the JSON literal ``null`` for None fields."""

    def test_model_dump_json_contains_null_for_optional_fields(self):
        item = _hit_to_item(_hit(_minimal_listing(), score=0.0))
        raw = item.model_dump_json()

        # Pydantic v2 emits no space after the colon: "backtest_sharpe":null.
        assert '"backtest_sharpe":null' in raw
        assert '"created_at":null' in raw

    def test_parsed_json_has_none_for_optional_fields(self):
        """Round-trip via json.loads so the assertion is space/format agnostic."""
        item = _hit_to_item(_hit(_minimal_listing(), score=0.0))
        parsed = json.loads(item.model_dump_json())

        assert parsed["backtest_sharpe"] is None
        assert parsed["created_at"] is None

    def test_model_dump_dict_has_none_for_optional_fields(self):
        item = _hit_to_item(_hit(_minimal_listing(), score=0.0))
        dumped = item.model_dump()

        assert dumped["backtest_sharpe"] is None
        assert dumped["created_at"] is None

    def test_none_optional_fields_are_not_omitted_from_json(self):
        """None must serialize to `null`, not be dropped from the body."""
        parsed = json.loads(
            _hit_to_item(_hit(_minimal_listing(), score=0.0)).model_dump_json()
        )
        assert "backtest_sharpe" in parsed
        assert "created_at" in parsed

    def test_sparse_listing_in_search_response_list_serializes_to_null(self):
        """Mirror the route's list comprehension; each item must serialize."""
        hits = [
            _hit(_minimal_listing("a"), score=0.5),
            _hit(_minimal_listing("b"), score=0.25),
        ]
        items = [_hit_to_item(h) for h in hits]

        for item in items:
            raw = item.model_dump_json()
            assert '"backtest_sharpe":null' in raw
            assert '"created_at":null' in raw
            parsed = json.loads(raw)
            assert parsed["backtest_sharpe"] is None
            assert parsed["created_at"] is None


# ---------------------------------------------------------------------------
# (c) Full passthrough round-trip for every enumerated field
# ---------------------------------------------------------------------------


class TestFieldPassthrough:
    """Every field enumerated by the review passes through ``_hit_to_item``
    unchanged — None for the Optional fields, real values for the rest."""

    def test_full_listing_passes_all_fields_through_unchanged(self):
        listing = _full_listing()
        item = _hit_to_item(_hit(listing, score=0.999))

        assert item.id == listing.id
        assert item.name == listing.name
        assert item.version == listing.version
        assert item.author == listing.author
        assert item.description == listing.description
        assert item.category == listing.category
        assert item.tags == list(listing.tags)
        assert item.rating == listing.rating
        assert item.downloads == listing.downloads
        assert item.backtest_sharpe == listing.backtest_sharpe
        assert item.min_capital == listing.min_capital
        assert item.created_at == listing.created_at
        # Score is rounded to 4dp by _hit_to_item.
        assert item.score == round(0.999, 4)

    def test_sparse_listing_passes_optional_none_and_numeric_defaults(self):
        listing = _minimal_listing()
        item = _hit_to_item(_hit(listing, score=0.0))

        # Optional fields: None passes through.
        assert item.backtest_sharpe is None
        assert item.created_at is None
        # Now-Optional numeric fields: their (default, non-None) values still
        # pass through unchanged because they are genuinely present in the
        # source data.
        assert item.rating == 0.0
        assert item.downloads == 0
        assert item.min_capital == 0.0
        assert item.description == ""
        # And those non-None values are NOT rendered as null in JSON.
        parsed = json.loads(item.model_dump_json())
        assert parsed["rating"] == 0.0
        assert parsed["downloads"] == 0
        assert parsed["min_capital"] == 0.0
        assert parsed["description"] == ""

    def test_missing_source_data_yields_none_not_zero(self):
        """When a listing carries None for the now-Optional numeric fields,
        ``_hit_to_item`` must surface ``None`` (the model default) — NOT a
        masked ``0``/``0.0`` placeholder.

        This is the core behavioural change behind making ``rating`` /
        ``downloads`` / ``min_capital`` Optional: genuinely-missing catalog
        data must serialize to the JSON literal ``null`` rather than a
        misleading zero that looks indistinguishable from a real zero value.
        """
        listing = StrategyListing(
            id="missing-1",
            name="Missing Strategy",
            version="0.1.0",
            author="Anonymous",
            description="d",
            category="algorithmic",
            tags=[],
            rating=None,
            downloads=None,
            backtest_sharpe=None,
            min_capital=None,
            created_at=None,
        )
        item = _hit_to_item(_hit(listing, score=0.0))

        # None source data -> None on the item (model default), never a zero.
        assert item.rating is None
        assert item.downloads is None
        assert item.min_capital is None
        assert item.backtest_sharpe is None
        assert item.created_at is None
        # And it serializes to JSON ``null``, not a masked 0 / 0.0.
        parsed = json.loads(item.model_dump_json())
        assert parsed["rating"] is None
        assert parsed["downloads"] is None
        assert parsed["min_capital"] is None

    def test_backtest_sharpe_none_vs_real_value_both_round_trip(self):
        none_item = _hit_to_item(_hit(_minimal_listing()))
        real_item = _hit_to_item(_hit(_full_listing()))

        assert none_item.backtest_sharpe is None
        assert real_item.backtest_sharpe == 1.53
        # JSON differs exactly on the null-vs-value distinction.
        assert json.loads(none_item.model_dump_json())["backtest_sharpe"] is None
        assert json.loads(real_item.model_dump_json())["backtest_sharpe"] == 1.53

    def test_created_at_none_vs_datetime_both_round_trip(self):
        none_item = _hit_to_item(_hit(_minimal_listing()))
        real_item = _hit_to_item(_hit(_full_listing()))

        assert none_item.created_at is None
        assert real_item.created_at == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
        parsed_none = json.loads(none_item.model_dump_json())
        parsed_real = json.loads(real_item.model_dump_json())
        assert parsed_none["created_at"] is None
        assert parsed_real["created_at"].startswith("2024-05-01T12:30:00")


# ---------------------------------------------------------------------------
# (d) Contract characterization: which fields are/are not nullable
# ---------------------------------------------------------------------------


class TestNullabilityContract:
    """Pin down precisely which fields tolerate ``None``.

    ``backtest_sharpe``, ``created_at``, ``rating``, ``downloads`` and
    ``min_capital`` are Optional and must accept None. The remaining
    review-enumerated field (``description``) is required on the model and
    rejects None — at the data layer it is a required ``str``. If a future
    change relaxes ``description`` to Optional (e.g. to tolerate missing
    catalog rows), these tests must be updated *deliberately* so the API
    shape change is reviewed, not accidental.
    """

    @pytest.mark.parametrize("field", OPTIONAL_FIELDS)
    def test_optional_field_accepts_none(self, field: str):
        base = {
            "id": "x",
            "name": "X",
            "version": "1.0.0",
            "author": "A",
            "description": "d",
            "category": "c",
            "tags": [],
            "rating": 0.0,
            "downloads": 0,
            "min_capital": 0.0,
        }
        base[field] = None
        try:
            SearchResultItem(**base)
        except ValidationError:
            pytest.fail(f"{field!r} is Optional and must accept None")

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_required_field_rejects_none(self, field: str):
        base = {
            "id": "x",
            "name": "X",
            "version": "1.0.0",
            "author": "A",
            "description": "d",
            "category": "c",
            "tags": [],
            "rating": 0.0,
            "downloads": 0,
            "min_capital": 0.0,
        }
        base[field] = None
        with pytest.raises(ValidationError):
            SearchResultItem(**base)


# ---------------------------------------------------------------------------
# (e) End-to-end sanity: a None-rich listing produces a fully valid item
# ---------------------------------------------------------------------------


class TestSparseListingEndToEnd:
    """A listing carrying only required fields must still produce a valid,
    JSON-serializable result item with ``null`` for the Optional fields."""

    def test_minimal_listing_round_trips_without_error(self):
        item = _hit_to_item(_hit(_minimal_listing(), score=1.0))
        # No ValidationError by reaching here; JSON must be parseable & valid.
        parsed = json.loads(item.model_dump_json())

        assert parsed["id"] == "sparse-1"
        assert parsed["name"] == "Sparse Strategy"
        assert parsed["backtest_sharpe"] is None
        assert parsed["created_at"] is None
        assert parsed["score"] == 1.0

    def test_empty_tags_list_round_trips_as_empty_array_not_null(self):
        # tags is a required list — an empty list must serialize to [], not null.
        item = _hit_to_item(_hit(_minimal_listing()))
        parsed = json.loads(item.model_dump_json())
        assert parsed["tags"] == []

    def test_score_rounding_preserved_with_null_optionals(self):
        item = _hit_to_item(_hit(_minimal_listing(), score=0.123456))
        assert item.score == round(0.123456, 4)
        assert json.loads(item.model_dump_json())["score"] == round(0.123456, 4)
