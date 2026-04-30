"""Abstract ingestion contract for reference-data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engine.reference.model import RefInstrument


@dataclass(frozen=True)
class IngestionResult:
    """Outcome of one sync run from a single adapter.

    ``errors`` is a tuple so the dataclass is genuinely immutable:
    callers cannot append after construction.
    """

    adapter: str
    fetched: int
    new: int
    updated: int
    errors: tuple[str, ...] = field(default_factory=tuple)


class IngestionAdapter(ABC):
    """Abstract contract every reference-data adapter implements."""

    name: str

    @abstractmethod
    async def fetch(self) -> Iterable[RefInstrument]:
        """Yield :class:`RefInstrument` records pulled from the source."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return ``True`` when the upstream is reachable."""


__all__ = ["IngestionAdapter", "IngestionResult"]
