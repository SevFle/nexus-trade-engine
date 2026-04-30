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

    MAX_QUERY_LEN = 64

    def search(
        self,
        query: str,
        *,
        asset_class: AssetClassLiteral | None = None,
        limit: int = 25,
    ) -> list[RefInstrument]:
        if not query or not query.strip():
            return []
        if len(query) > self.MAX_QUERY_LEN:
            return []
        q = query.strip().lower()
        scored: list[tuple[int, RefInstrument]] = []
        for rec in self._records:
            if asset_class is not None and rec.asset_class != asset_class:
                continue
            score = self._score(rec, q)
            if score > 0:
                scored.append((score, rec))
        # Use heap-based top-k so 100k records with many partial matches
        # stay O(n log limit) rather than O(n log n).
        import heapq  # noqa: PLC0415 - local import keeps cold-path stdlib out of hot path

        top = heapq.nlargest(limit, scored, key=lambda t: t[0])
        return [rec for _, rec in top]

    @staticmethod
    def _score(rec: RefInstrument, q: str) -> int:  # noqa: PLR0911 - one return per scoring tier
        """Rank both ticker and company-name matches.

        Both symbol-style queries (e.g. ``AAPL``) and company-name
        queries (e.g. ``Apple``) reach the target instrument; the
        caller does not have to know which form the catalog stores.

        Tiers (high -> low):

        - 100: ticker exact match
        - 90:  name exact match
        - 80:  ticker prefix match
        - 70:  name prefix match
        - 60:  ticker contains q
        - 50:  any name token starts with q (word-prefix)
        - 25:  q anywhere inside name
        """
        ticker = rec.primary_ticker.lower()
        name = rec.name.lower()
        if ticker == q:
            return 100
        if name == q:
            return 90
        if ticker.startswith(q):
            return 80
        if name.startswith(q):
            return 70
        if q in ticker:
            return 60
        # Word-token prefix: catches "Berk" -> "Berkshire" inside
        # "Berkshire Hathaway Inc." even when the ticker is BRK.B.
        for token in _tokenize_name(name):
            if token.startswith(q):
                return 50
        if q in name:
            return 25
        return 0


def _tokenize_name(name: str) -> list[str]:
    """Split a name into lowercase alphanumeric word tokens."""
    out: list[str] = []
    current: list[str] = []
    for ch in name:
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


__all__ = ["SearchIndex"]
