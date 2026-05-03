"""Reference data search + typeahead endpoints.

Public surface:

- ``GET /reference/suggest?q=<query>&limit=<n>&asset_class=<cls>``
  Typeahead-friendly: queries the Yahoo Finance search API for real-time
  results across all asset classes (equities, ETFs, crypto, forex, etc.).
  Falls back to the local :class:`SearchIndex` when the external call
  fails or returns no results.

The :class:`SearchIndex` instance is a process-singleton injected via
:func:`get_search_index`. Production wires it once at app startup;
tests inject a seeded fixture via FastAPI's dependency overrides.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
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

_YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_YAHOO_SEARCH_TIMEOUT = 5.0

_INDEX: SearchIndex | None = None


def get_search_index() -> SearchIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = SearchIndex()
    return _INDEX


def _serialize(suggestion: Suggestion) -> dict[str, object]:
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


def _serialize_yahoo(item: dict[str, Any]) -> dict[str, object]:
    symbol = item.get("symbol", "")
    name = item.get("shortname") or item.get("longname") or item.get("name", "")
    quote_type = item.get("quoteType", "")
    exchange = item.get("exchange", "")
    asset_class = _map_quote_type(quote_type)
    return {
        "symbol": symbol,
        "name": name,
        "display": f"{symbol} — {name}" if name else symbol,
        "completion": name or symbol,
        "score": 80 if quote_type == "EQUITY" else 60,
        "record": {
            "id": "",
            "primary_ticker": symbol,
            "primary_venue": exchange,
            "asset_class": asset_class,
            "name": name,
            "currency": item.get("currency", "USD"),
        },
    }


def _map_quote_type(quote_type: str) -> str:
    mapping = {
        "EQUITY": "equity",
        "ETF": "etf",
        "MUTUALFUND": "etf",
        "CRYPTOCURRENCY": "crypto",
        "CURRENCY": "forex",
        "INDEX": "etf",
        "FUTURE": "future",
        "OPTION": "option",
    }
    return mapping.get(quote_type, "equity")


async def _yahoo_search(query: str, limit: int) -> list[dict[str, object]]:
    try:
        async with httpx.AsyncClient(timeout=_YAHOO_SEARCH_TIMEOUT) as client:
            resp = await client.get(
                _YAHOO_SEARCH_URL,
                params={
                    "q": query,
                    "quotesCount": limit,
                    "newsCount": 0,
                    "enableFuzzyQuery": "true",
                    "quotesQueryId": "tts_match",
                },
                headers={"User-Agent": "nexus-trade-engine/1.0"},
            )
            if resp.status_code != 200:
                logger.warning("reference.yahoo_search.http_error", status=resp.status_code)
                return []
            body = resp.json()
            items = body.get("quotes", []) or []
            results = []
            for item in items:
                symbol = item.get("symbol", "")
                if not symbol or "symbol" not in item:
                    continue
                if item.get("quoteType") in ("NONE", None):
                    continue
                results.append(_serialize_yahoo(item))
                if len(results) >= limit:
                    break
            return results
    except (httpx.TimeoutException, httpx.RequestError):
        logger.warning("reference.yahoo_search.timeout_or_error")
        return []
    except Exception:
        logger.exception("reference.yahoo_search.unexpected_error")
        return []


@router.get("/suggest")
async def suggest(
    q: str = Query(..., description="User-typed query (ticker or name fragment)"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, description="Maximum results"),
    asset_class: AssetClassLiteral | None = Query(
        None, description="Optional asset-class filter"
    ),
    index: SearchIndex = Depends(get_search_index),
) -> dict[str, list[dict[str, object]]]:
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

    local = index.suggest(q, asset_class=asset_class, limit=capped_limit)
    if local:
        return {"suggestions": [_serialize(s) for s in local]}

    yahoo_results = await _yahoo_search(q.strip(), capped_limit)

    if asset_class is not None:
        yahoo_results = [r for r in yahoo_results if r.get("record", {}).get("asset_class") == asset_class]

    if yahoo_results:
        return {"suggestions": yahoo_results[:capped_limit]}

    return {"suggestions": []}


__all__ = ["get_search_index", "router"]
