"""Reference-data exception hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.reference.model import RefInstrument


class ReferenceError(Exception):  # noqa: A001 - domain name, not the builtin
    """Base class for reference-data errors."""


class AmbiguousSymbolError(ReferenceError):
    """Raised when a query resolves to more than one instrument."""

    def __init__(self, query: object, candidates: list[RefInstrument]) -> None:
        self.query = query
        self.candidates = candidates
        super().__init__(
            f"ambiguous symbol {query!r}: {len(candidates)} candidates"
        )


__all__ = ["AmbiguousSymbolError", "ReferenceError"]
