"""Reference data search + typeahead endpoints.

Public surface:

- ``GET /reference/suggest?q=<query>&limit=<n>&asset_class=<cls>``
  Typeahead-friendly: prefix-first ranking, single-character queries
  accepted, default limit 10, Levenshtein-1 fuzzy fallback when no
  prefix match exists. Returns a list of ``Suggestion`` objects, each
  with ``completion`` (the matched fragment, suitable for highlighting
  in a dropdown), ``score`` (the underlying tier weight), and
  ``record`` (the full :class:`RefInstrument`).

The :class:`SearchIndex` instance is a process-singleton injected via
:func:`get_search_index`. Production wires it once at app startup;
tests inject a seeded fixture via FastAPI's dependency overrides.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from engine.reference.model import AssetClassLiteral
from engine.reference.search import SearchIndex

if TYPE_CHECKING:
    from engine.reference.search import Suggestion

router = APIRouter()
logger = structlog.get_logger()

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50
_MAX_QUERY_LEN = SearchIndex.MAX_QUERY_LEN

_INDEX: SearchIndex | None = None


def get_search_index() -> SearchIndex:
    """Return the process-singleton :class:`SearchIndex`.

    Production wires the singleton once at startup. Tests override this
    dependency via ``app.dependency_overrides[get_search_index]`` to
    inject a seeded fixture.
    """
    global _INDEX  # noqa: PLW0603 - process-wide singleton initialized lazily
    if _INDEX is None:
        _INDEX = SearchIndex()
    return _INDEX


def _serialize(suggestion: Suggestion) -> dict[str, object]:
    """Surface symbol + company name at the top of the suggestion.

    The frontend dropdown row needs both the ticker (e.g. ``AAPL``) and
    the company name (``Apple Inc.``) regardless of which one matched
    the user's query — a typeahead row that says only ``Apple`` without
    showing ``AAPL`` is unusable for placing a trade. ``display`` is a
    pre-formatted ``SYMBOL — Name`` string so the frontend does not
    have to assemble it.
    """
    rec = suggestion.record
    return {
        "symbol": rec.primary_ticker,
        "name": rec.name,
        "display": f"{rec.primary_ticker} — {rec.name}",
        "completion": suggestion.completion,
        "score": suggestion.score,
        "record": {
            "id": str(rec.id),
            "primary_ticker": rec.primary_ticker,
            "primary_venue": rec.primary_venue,
            "asset_class": rec.asset_class,
            "name": rec.name,
            "currency": rec.currency,
        },
    }


@router.get("/suggest")
async def suggest(
    q: str = Query(..., description="User-typed query (ticker or name fragment)"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, description="Maximum results"),
    asset_class: AssetClassLiteral | None = Query(
        None, description="Optional asset-class filter"
    ),
    index: SearchIndex = Depends(get_search_index),
) -> dict[str, list[dict[str, object]]]:
    """Typeahead suggestions for ticker / company-name queries.

    Empty / whitespace-only / over-long queries return 400 so a misuse
    surfaces during integration rather than as silent zero results.
    """
    if not q or not q.strip():
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="query must be non-empty",
        )
    if len(q) > _MAX_QUERY_LEN:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"query exceeds {_MAX_QUERY_LEN} chars",
        )
    capped_limit = min(limit, _MAX_LIMIT)
    out = index.suggest(q, asset_class=asset_class, limit=capped_limit)
    return {"suggestions": [_serialize(s) for s in out]}


__all__ = ["get_search_index", "router"]
