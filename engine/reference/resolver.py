"""In-memory :class:`Resolver` — the canonical symbol → instrument lookup.

Backs onto Python dicts so it is dependency-free and suitable for
bootstrap, tests, and small caches. The DB-backed resolver implements
the same surface against Postgres in a follow-up issue.

Resolution order for a free-form string query:

1. Empty / whitespace / suspicious garbage → ``None``
2. ``TICKER.SUFFIX`` (e.g. ``AAPL.L``) → suffix mapped to MIC
3. Exact ticker on a unique listing → that instrument
4. Multiple matches → :class:`AmbiguousSymbolError`

Dict-form queries (``{"ticker": ..., "venue": ...}`` or ``{"isin": ...}``)
are deterministic — they either match exactly one or return ``None``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from engine.reference.exceptions import AmbiguousSymbolError

if TYPE_CHECKING:
    from engine.reference.model import RefInstrument


_SUFFIX_TO_MIC: dict[str, str] = {
    "L": "XLON",
    "TO": "XTSE",
    "PA": "XPAR",
    "DE": "XETR",
    "F": "XETR",
    "MI": "XMIL",
    "AS": "XAMS",
    "T": "XTKS",
    "HK": "XHKG",
    "SS": "XSHG",
    "SZ": "XSHE",
    "SW": "XSWX",
    "ST": "XSTO",
    "OL": "XOSL",
    "VI": "XWBO",
    "MX": "XMEX",
    "BR": "XBRU",
    "JO": "XJSE",
}

_MAX_QUERY_LEN = 64
_SUSPICIOUS = frozenset({"<", ">", "&", "\x00"})


def _looks_garbage(raw: str) -> bool:
    if not raw or not raw.strip():
        return True
    if len(raw) > _MAX_QUERY_LEN:
        return True
    return any(c in _SUSPICIOUS for c in raw)


class Resolver:
    """Symbol-master with multi-key lookup."""

    def __init__(self) -> None:
        self._by_id: dict[Any, RefInstrument] = {}
        self._by_ticker_venue: dict[tuple[str, str], RefInstrument] = {}
        self._by_ticker: dict[str, list[RefInstrument]] = defaultdict(list)
        self._by_isin: dict[str, RefInstrument] = {}
        self._by_cusip: dict[str, RefInstrument] = {}
        self._by_figi: dict[str, RefInstrument] = {}
        self._by_cik: dict[str, RefInstrument] = {}

    def register(self, inst: RefInstrument) -> None:
        """Add an instrument to the catalog and rebuild its indexes."""
        self._by_id[inst.id] = inst
        self._by_ticker[inst.primary_ticker.upper()].append(inst)
        self._by_ticker_venue[(inst.primary_ticker.upper(), inst.primary_venue)] = inst
        for listing in inst.listings:
            self._by_ticker_venue[(listing.ticker.upper(), listing.venue)] = inst
            self._by_ticker[listing.ticker.upper()].append(inst)
        if inst.ids.isin:
            self._by_isin[inst.ids.isin] = inst
        if inst.ids.cusip:
            self._by_cusip[inst.ids.cusip] = inst
        if inst.ids.figi:
            self._by_figi[inst.ids.figi] = inst
        if inst.ids.cik:
            self._by_cik[inst.ids.cik] = inst

    def resolve(self, query: str | dict[str, Any]) -> RefInstrument | None:
        """Look up an instrument by ticker, dotted suffix, or ID dict."""
        if isinstance(query, dict):
            return self._resolve_dict(query)
        if not isinstance(query, str):
            msg = f"resolve query must be str or dict, got {type(query).__name__}"
            raise TypeError(msg)
        if _looks_garbage(query):
            return None
        return self._resolve_string(query.strip())

    def _resolve_dict(self, q: dict[str, Any]) -> RefInstrument | None:  # noqa: PLR0911 - one return per id type
        if "isin" in q:
            return self._by_isin.get(q["isin"])
        if "cusip" in q:
            return self._by_cusip.get(q["cusip"])
        if "figi" in q:
            return self._by_figi.get(q["figi"])
        if "cik" in q:
            return self._by_cik.get(q["cik"])
        if "ticker" in q and "venue" in q:
            return self._by_ticker_venue.get((str(q["ticker"]).upper(), q["venue"]))
        if "ticker" in q:
            return self._resolve_string(str(q["ticker"]))
        return None

    def _resolve_string(self, raw: str) -> RefInstrument | None:
        if "." in raw:
            ticker, _, suffix = raw.rpartition(".")
            mic = _SUFFIX_TO_MIC.get(suffix.upper())
            if mic and ticker:
                hit = self._by_ticker_venue.get((ticker.upper(), mic))
                if hit is not None:
                    return hit
        hits = self._by_ticker.get(raw.upper(), [])
        unique_by_id: dict[Any, RefInstrument] = {h.id: h for h in hits}
        candidates = list(unique_by_id.values())
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        raise AmbiguousSymbolError(query=raw, candidates=candidates)


__all__ = ["Resolver"]
