"""Keyword search & filtering service for the marketplace strategy catalog.

Backs the ``GET /api/marketplace/search`` endpoint. It is intentionally
persistence-agnostic — the public contract is the small :class:`StrategyCatalog`
protocol, with a thread-safe in-memory default implementation. A SQLAlchemy
(or remote-API) backend can later be dropped in behind the protocol without
touching the route layer, mirroring the layout of :mod:`engine.marketplace.ratings`.

Matching model
--------------
* The query is tokenised on non-alphanumeric boundaries and lower-cased, so
  ``"Mean-Reversion"`` becomes ``["mean", "reversion"]``. Matching is
  case-insensitive everywhere.
* A listing scores points per token depending on *where* it matches, with
  name hits weighted highest, then tags, then author, then description. A
  name that exactly equals a token earns a bonus. See :data:`FIELD_WEIGHTS`.
* With a non-empty query a listing is kept only when its total score is
  ``> 0`` (i.e. at least one token matched somewhere) — OR semantics across
  tokens, with relevance ranking rewarding listings that match more of them.
* With an empty/absent query no keyword filter is applied: every listing is
  a candidate and ordering is driven purely by ``sort`` (defaulting to
  ``downloads`` because a relevance score of 0 for everything would be a
  meaningless no-op ordering).

Sorting
-------
``relevance`` (score → downloads → name), ``downloads`` (desc), ``rating``
(desc), ``name`` (asc, case-insensitive) and ``newest`` (created_at desc).
Every ordering falls back to a stable case-insensitive name tiebreak so
equal-keyed listings never flip between pages (which would otherwise make
pagination non-deterministic).

Pagination
----------
1-indexed ``page`` with a clamped ``limit``. ``total`` is the count of *all*
matches (pre-pagination) and ``has_more`` reports whether a subsequent page
exists, so a UI can render "load more" without re-counting.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

_logger = logging.getLogger(__name__)

# Weights applied per token that matches a given field. Name hits dominate so
# a strategy literally called "momentum" outranks one that merely mentions it
# in its description, which matches how users expect marketplace search to
# behave.
FIELD_WEIGHTS: dict[str, float] = {
    "name": 10.0,
    "tags": 5.0,
    "author": 3.0,
    "description": 1.0,
}

# Exact ``name == token`` bonus, stacked on top of the name weight.
EXACT_NAME_BONUS: float = 5.0

# Sort keys accepted by :meth:`StrategyCatalog.search`. Order matters only for
# the default-selection logic in the route layer; the store validates against
# this tuple.
ALLOWED_SORTS: tuple[str, ...] = (
    "relevance",
    "downloads",
    "rating",
    "name",
    "newest",
)

DEFAULT_SORT: str = "relevance"
# When the query is empty ``relevance`` is meaningless (every score is 0), so
# the route layer falls back to this sort for a useful default ordering.
EMPTY_QUERY_FALLBACK_SORT: str = "downloads"

DEFAULT_LIMIT: int = 20
MIN_LIMIT: int = 1
MAX_LIMIT: int = 100

# Splits on any run of non-alphanumeric characters so hyphenated / punctuated
# terms ("mean-reversion", "vix!") tokenise predictably into ["mean",
# "reversion"] / ["vix"]. Compiled once at import.
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


class SearchError(ValueError):
    """Raised when a search request fails validation.

    Carries a human-readable ``detail`` suitable for surfacing directly in an
    HTTP 400 error response.
    """


@dataclass
class StrategyListing:
    """A searchable marketplace strategy entry."""

    id: str
    name: str
    version: str
    author: str
    description: str
    category: str
    tags: list[str] = field(default_factory=list)
    rating: float = 0.0
    downloads: int = 0
    backtest_sharpe: float | None = None
    min_capital: float = 0.0
    created_at: datetime | None = None


@dataclass
class SearchHit:
    """A listing paired with its computed relevance score."""

    listing: StrategyListing
    score: float = 0.0


@dataclass
class SearchPage:
    """A page of search results plus pagination metadata."""

    results: list[SearchHit]
    total: int
    page: int
    limit: int
    has_more: bool
    sort: str
    query: str


class StrategyCatalog(Protocol):
    """Persistence-agnostic strategy-catalog search contract."""

    def search(
        self,
        query: str | None = None,
        *,
        category: str | None = None,
        tag: str | None = None,
        sort: str = DEFAULT_SORT,
        page: int = 1,
        limit: int = DEFAULT_LIMIT,
    ) -> SearchPage:
        """Search the catalog, returning one page of ranked results."""
        ...

    def add(self, listing: StrategyListing) -> None:
        """Insert (or replace, by ``id``) a listing. Used for seeding/tests."""
        ...

    def reset(self) -> None:
        """Drop every listing. Used to isolate tests from seeded defaults."""
        ...


def _tokenize(query: str | None) -> list[str]:
    """Lower-case the query and split it into alphanumeric tokens."""
    if not query:
        return []
    return [tok for tok in _TOKEN_SPLIT.split(query.lower()) if tok]


def _score_strategy(listing: StrategyListing, tokens: list[str]) -> float:
    """Compute a relevance score for ``listing`` against ``tokens``.

    Returns ``0.0`` when ``tokens`` is empty (no query) so the caller can treat
    a positive score as "matched at least one token".
    """
    if not tokens:
        return 0.0

    name_l = listing.name.lower()
    desc_l = listing.description.lower()
    author_l = listing.author.lower()
    tags_l = [t.lower() for t in listing.tags]

    score = 0.0
    for tok in tokens:
        if tok in name_l:
            score += FIELD_WEIGHTS["name"]
            if name_l == tok:
                # An exact name match is the strongest possible signal — a
                # user searching the strategy's literal name expects it first.
                score += EXACT_NAME_BONUS
        if tok in tags_l:
            score += FIELD_WEIGHTS["tags"]
        elif any(tok in t for t in tags_l):
            # Partial tag match (e.g. "moment" inside "momentum") still counts,
            # but at half weight so exact tag matches rank above it.
            score += FIELD_WEIGHTS["tags"] * 0.5
        if tok in author_l:
            score += FIELD_WEIGHTS["author"]
        if tok in desc_l:
            score += FIELD_WEIGHTS["description"]
    return score


def _order(hits: list[SearchHit], sort: str) -> list[SearchHit]:
    """Return ``hits`` ordered by ``sort`` with a stable name tiebreak.

    The name tiebreak (case-insensitive, ascending) is applied first via a
    stable sort, then the primary key is applied with a *second* stable sort.
    Python's sort is stable, so within equal primary keys the name ordering is
    preserved — guaranteeing deterministic pagination across page boundaries.
    """
    # Ultimate tiebreak: name ascending (case-insensitive).
    ordered = sorted(hits, key=lambda h: h.listing.name.lower())
    if sort == "name":
        return ordered
    if sort == "downloads":
        ordered.sort(key=lambda h: h.listing.downloads, reverse=True)
    elif sort == "rating":
        ordered.sort(key=lambda h: h.listing.rating, reverse=True)
    elif sort == "newest":
        # created_at desc; None created_at sorts last (treated as oldest).
        ordered.sort(
            key=lambda h: h.listing.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
    else:
        # "relevance": score desc (the name tiebreak already breaks the rest).
        ordered.sort(key=lambda h: h.score, reverse=True)
    return ordered


class InMemoryStrategyCatalog:
    """Thread-safe in-memory implementation of :class:`StrategyCatalog`.

    Listings are keyed by ``id`` so :meth:`add` is idempotent (re-adding the
    same id replaces the entry rather than duplicating it), which keeps seeded
    demo data and test fixtures from accumulating across re-runs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listings: dict[str, StrategyListing] = {}

    def add(self, listing: StrategyListing) -> None:
        if not listing.id:
            raise SearchError("listing.id must be a non-empty string")
        with self._lock:
            self._listings[listing.id] = listing

    def add_many(self, listings: list[StrategyListing]) -> None:
        for listing in listings:
            self.add(listing)

    def reset(self) -> None:
        with self._lock:
            self._listings.clear()

    def search(
        self,
        query: str | None = None,
        *,
        category: str | None = None,
        tag: str | None = None,
        sort: str = DEFAULT_SORT,
        page: int = 1,
        limit: int = DEFAULT_LIMIT,
    ) -> SearchPage:
        if sort not in ALLOWED_SORTS:
            raise SearchError(
                f"unknown sort: {sort!r}; must be one of {list(ALLOWED_SORTS)}"
            )
        if page < 1:
            raise SearchError("page must be >= 1")
        if limit < MIN_LIMIT:
            raise SearchError(f"limit must be >= {MIN_LIMIT}")
        if limit > MAX_LIMIT:
            raise SearchError(f"limit must be <= {MAX_LIMIT}")

        tokens = _tokenize(query)
        cat_l = category.strip().lower() if category and category.strip() else None
        tag_l = tag.strip().lower() if tag and tag.strip() else None

        with self._lock:
            listings = list(self._listings.values())

        hits: list[SearchHit] = []
        for listing in listings:
            if cat_l is not None and listing.category.lower() != cat_l:
                continue
            if tag_l is not None and not any(
                tag_l == t.lower() for t in listing.tags
            ):
                continue
            score = _score_strategy(listing, tokens)
            # Only apply the keyword filter when a query was supplied — an
            # empty query means "browse everything", not "match nothing".
            if tokens and score <= 0.0:
                continue
            hits.append(SearchHit(listing=listing, score=score))

        ordered = _order(hits, sort)

        total = len(ordered)
        start = (page - 1) * limit
        page_hits = ordered[start : start + limit]
        has_more = (start + limit) < total

        return SearchPage(
            results=page_hits,
            total=total,
            page=page,
            limit=limit,
            has_more=has_more,
            sort=sort,
            query=query or "",
        )


# ---------------------------------------------------------------------------
# Built-in demo catalog
# ---------------------------------------------------------------------------
#
# The marketplace does not yet have a persistent registry backend, so the
# default in-memory catalog is seeded with a small, representative set of
# strategies covering every category exposed by ``GET /categories``. This keeps
# the search endpoint useful out-of-the-box (e.g. during local development and
# smoke testing) without coupling it to a database. Tests override the
# ``get_strategy_catalog`` dependency with a controlled catalog, so this seed
# data never leaks into assertions.

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _default_strategies() -> list[StrategyListing]:
    """Build the built-in demo catalog with staggered ``created_at`` values.

    Re-computed on each call (rather than a module-level constant) so callers
    always receive fresh objects and mutation of one default listing cannot
    bleed into another consumer of the default catalog.
    """
    return [
        StrategyListing(
            id="mean-reversion-equity",
            name="Mean Reversion Equity",
            version="1.2.0",
            author="Nexus Labs",
            description="Statistical mean-reversion on single-name equities "
            "using Bollinger bands and RSI confirmation.",
            category="algorithmic",
            tags=["mean-reversion", "equities", "rsi"],
            rating=4.5,
            downloads=1240,
            backtest_sharpe=1.42,
            min_capital=10000.0,
            created_at=_BASE,
        ),
        StrategyListing(
            id="momentum-breakout",
            name="Momentum Breakout",
            version="0.9.3",
            author="QuantDesk",
            description="Breakout momentum strategy trading range expansions "
            "on high-liquid futures with volume confirmation.",
            category="algorithmic",
            tags=["momentum", "breakout", "futures"],
            rating=4.1,
            downloads=980,
            backtest_sharpe=1.18,
            min_capital=25000.0,
            created_at=_BASE + timedelta(days=10),
        ),
        StrategyListing(
            id="lstm-sentiment",
            name="LSTM Sentiment Trader",
            version="2.0.1",
            author="DeepSignals",
            description="Bidirectional LSTM that predicts next-bar direction "
            "from news sentiment embeddings and price features.",
            category="ml",
            tags=["lstm", "sentiment", "nlp"],
            rating=4.3,
            downloads=760,
            backtest_sharpe=1.05,
            min_capital=50000.0,
            created_at=_BASE + timedelta(days=20),
        ),
        StrategyListing(
            id="gpt-macro-regime",
            name="GPT Macro Regime",
            version="0.4.0",
            author="Nexus Labs",
            description="LLM-driven macro regime classifier that tilts a "
            "sector ETF rotation portfolio based on central-bank commentary.",
            category="llm",
            tags=["macro", "gpt", "regime", "etf"],
            rating=3.9,
            downloads=410,
            backtest_sharpe=0.86,
            min_capital=100000.0,
            created_at=_BASE + timedelta(days=30),
        ),
        StrategyListing(
            id="dividend-wheel",
            name="Dividend Wheel",
            version="1.5.0",
            author="IncomeCraft",
            description="Covered-call wheel strategy on dividend-paying "
            "blue chips for consistent options income.",
            category="income",
            tags=["options", "dividend", "income"],
            rating=4.6,
            downloads=1530,
            backtest_sharpe=1.31,
            min_capital=15000.0,
            created_at=_BASE + timedelta(days=40),
        ),
        StrategyListing(
            id="pairs-cointegration",
            name="Pairs Cointegration",
            version="1.0.2",
            author="QuantDesk",
            description="Statistical arbitrage of cointegrated equity pairs "
            "with dynamic hedge ratio estimation.",
            category="algorithmic",
            tags=["pairs", "statistical", "mean-reversion"],
            rating=4.0,
            downloads=620,
            backtest_sharpe=1.22,
            min_capital=50000.0,
            created_at=_BASE + timedelta(days=50),
        ),
        StrategyListing(
            id="ensemble-regime-switch",
            name="Ensemble Regime Switch",
            version="0.7.0",
            author="DeepSignals",
            description="Hybrid ensemble that blends momentum and "
            "mean-reversion sub-strategies gated by a hidden Markov regime model.",
            category="hybrid",
            tags=["ensemble", "regime", "momentum", "mean-reversion"],
            rating=4.2,
            downloads=540,
            backtest_sharpe=1.15,
            min_capital=75000.0,
            created_at=_BASE + timedelta(days=60),
        ),
        StrategyListing(
            id="vix-volatility-arb",
            name="VIX Volatility Arbitrage",
            version="0.3.1",
            author="VolEdge",
            description="Term-structure arbitrage on VIX futures capturing "
            "contango/backwardation roll yield.",
            category="macro",
            tags=["volatility", "arbitrage", "vix", "futures"],
            rating=3.7,
            downloads=330,
            backtest_sharpe=0.94,
            min_capital=100000.0,
            created_at=_BASE + timedelta(days=70),
        ),
    ]


# Process-singleton state, mirroring the ratings-store pattern. A dict is used
# (rather than a bare module global) so :func:`reset_default_catalog` can mutate
# it in place and any holder of the previously-returned catalog sees the reset
# reflected through the shared lock.
_default_state: dict[str, InMemoryStrategyCatalog | None] = {"catalog": None}
_default_lock = threading.Lock()


def get_strategy_catalog() -> InMemoryStrategyCatalog:
    """Return the process-wide default :class:`InMemoryStrategyCatalog`.

    Lazily instantiated and memoised under :data:`_default_lock`, then seeded
    once with the built-in demo catalog (:func:`_default_strategies`) so the
    search endpoint returns representative data out of the box.
    """
    with _default_lock:
        if _default_state["catalog"] is None:
            catalog = InMemoryStrategyCatalog()
            catalog.add_many(_default_strategies())
            _default_state["catalog"] = catalog
        return _default_state["catalog"]


def reset_default_catalog() -> None:
    """Clear every listing from the default catalog.

    Used to isolate tests that opt into the default singleton from prior seed
    data. Does *not* re-seed — call :func:`get_strategy_catalog` afterwards to
    observe an empty catalog, or rebuild a fresh catalog via
    :class:`InMemoryStrategyCatalog`.
    """
    get_strategy_catalog().reset()
