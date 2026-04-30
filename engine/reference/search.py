"""In-memory typeahead :class:`SearchIndex`.

Linear scan with substring matching plus an optional asset-class filter.
Sufficient for the bootstrap path; the production index will sit behind
Postgres ``pg_trgm`` in a follow-up issue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.reference.model import AssetClassLiteral, RefInstrument


class SearchIndex:
    """Append-only in-memory index over :class:`RefInstrument` records."""

    def __init__(self) -> None:
        self._records: list[RefInstrument] = []

    def add(self, inst: RefInstrument) -> None:
        self._records.append(inst)

    def search(
        self,
        query: str,
        *,
        asset_class: AssetClassLiteral | None = None,
        limit: int = 25,
    ) -> list[RefInstrument]:
        if not query or not query.strip():
            return []
        q = query.strip().lower()
        out: list[tuple[int, RefInstrument]] = []
        for rec in self._records:
            if asset_class is not None and rec.asset_class != asset_class:
                continue
            score = self._score(rec, q)
            if score > 0:
                out.append((score, rec))
        out.sort(key=lambda t: -t[0])
        return [rec for _, rec in out[:limit]]

    @staticmethod
    def _score(rec: RefInstrument, q: str) -> int:
        ticker = rec.primary_ticker.lower()
        name = rec.name.lower()
        if ticker == q:
            return 100
        if ticker.startswith(q):
            return 75
        if q in ticker:
            return 50
        if q in name:
            return 25
        return 0


__all__ = ["SearchIndex"]
