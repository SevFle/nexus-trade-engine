"""In-memory typeahead :class:`SearchIndex`.

Linear scan with substring matching plus an optional asset-class filter.
Sufficient for the bootstrap path; the production index will sit behind
Postgres ``pg_trgm`` in a follow-up issue.

Two entry points:

- :meth:`SearchIndex.search` — general ranked search. Symbol-style and
  company-name queries both reach the target instrument; the caller does
  not need to know which form the catalog stores.
- :meth:`SearchIndex.suggest` — typeahead-optimized variant. Prefers
  prefix completions, accepts single-character queries, and falls back
  to one-edit-distance fuzzy matching on tokens only when no
  prefix/substring hit is found. Default limit is small enough for a
  typeahead dropdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.reference.model import AssetClassLiteral, RefInstrument


@dataclass(frozen=True)
class Suggestion:
    """One typeahead suggestion: the matched record + presentational hint."""

    record: RefInstrument
    completion: str
    score: int


class SearchIndex:
    """Append-only in-memory index over :class:`RefInstrument` records."""

    def __init__(self) -> None:
        self._records: list[RefInstrument] = []

    def add(self, inst: RefInstrument) -> None:
        self._records.append(inst)

    MAX_QUERY_LEN = 64
    DEFAULT_SUGGEST_LIMIT = 10

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

    def suggest(
        self,
        query: str,
        *,
        asset_class: AssetClassLiteral | None = None,
        limit: int | None = None,
    ) -> list[Suggestion]:
        """Typeahead suggestions: prefix-first, fuzzy as fallback.

        Tiers (high → low):

        - 100 ticker exact
        - 90  name exact
        - 80  ticker prefix
        - 70  name token prefix (any whitespace-separated word in name)
        - 60  ticker contains
        - 25  name contains
        - 15  fuzzy (Levenshtein distance 1) on a name token or ticker

        Fuzzy is computed only if no record hit a higher tier, so a
        clean prefix never gets diluted by typo candidates.
        """
        if not query or not query.strip():
            return []
        if len(query) > self.MAX_QUERY_LEN:
            return []
        q = query.strip().lower()
        cap = limit if limit is not None else self.DEFAULT_SUGGEST_LIMIT
        primary: list[Suggestion] = []
        for rec in self._records:
            if asset_class is not None and rec.asset_class != asset_class:
                continue
            score, completion = self._suggest_score(rec, q)
            if score > 0:
                primary.append(Suggestion(record=rec, completion=completion, score=score))
        if primary:
            primary.sort(key=lambda s: -s.score)
            return primary[:cap]
        # No prefix or substring hits — try fuzzy on tokens.
        fuzzy: list[Suggestion] = []
        for rec in self._records:
            if asset_class is not None and rec.asset_class != asset_class:
                continue
            completion = self._fuzzy_match(rec, q)
            if completion is not None:
                fuzzy.append(Suggestion(record=rec, completion=completion, score=15))
        return fuzzy[:cap]

    @staticmethod
    def _suggest_score(rec: RefInstrument, q: str) -> tuple[int, str]:  # noqa: PLR0911 - one return per scoring tier
        """Score for typeahead. Returns (score, completion) or (0, '')."""
        ticker = rec.primary_ticker.lower()
        name = rec.name.lower()
        if ticker == q:
            return 100, rec.primary_ticker
        if name == q:
            return 90, rec.name
        if ticker.startswith(q):
            return 80, rec.primary_ticker
        tokens = _tokenize_name(name)
        for token in tokens:
            if token == q:
                return 78, token
        # First-token prefix beats later-token prefix so "Microsoft Corp."
        # ranks above "ZZZ has microsoft inside" for query "Micr".
        if tokens and tokens[0].startswith(q):
            return 75, tokens[0]
        for token in tokens:
            if token.startswith(q):
                return 70, token
        if q in ticker:
            return 60, rec.primary_ticker
        if q in name:
            return 25, rec.name
        return 0, ""

    @staticmethod
    def _fuzzy_match(rec: RefInstrument, q: str) -> str | None:
        """Levenshtein distance ≤ 1 against any name token or ticker.

        Returns the matched token (so callers can show the completion)
        or None when no token is within distance 1.
        """
        candidates = [rec.primary_ticker.lower(), *_tokenize_name(rec.name.lower())]
        for cand in candidates:
            if _within_one_edit(q, cand):
                return cand
        return None

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


def _within_one_edit(a: str, b: str) -> bool:
    """True iff a and b differ by at most one insertion, deletion, or substitution.

    Linear-time short-circuit: bail out as soon as a second mismatch
    appears. Only meaningful for short queries (typeahead) — do not use
    on long strings.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    # Ensure a is the shorter (or equal) one to simplify.
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = 0
    edits = 0
    while i < la and j < lb:
        if a[i] != b[j]:
            edits += 1
            if edits > 1:
                return False
            if la == lb:
                # Substitution — advance both.
                i += 1
                j += 1
            else:
                # Insertion in b — advance only b.
                j += 1
        else:
            i += 1
            j += 1
    # Trailing extra char in b (insertion at end) is one edit.
    if j < lb:
        edits += 1
    return edits <= 1


__all__ = ["SearchIndex", "Suggestion"]
