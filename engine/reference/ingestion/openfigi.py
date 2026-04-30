"""OpenFIGI ingestion adapter (skeleton).

Full implementation hits ``https://api.openfigi.com/v3/mapping`` to
batch-resolve identifiers. PR1 ships only the class structure so the
ingestion contract can be exercised; live HTTP wiring lands in the
follow-up issue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.reference.ingestion.base import IngestionAdapter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engine.reference.model import RefInstrument


class OpenFIGIAdapter(IngestionAdapter):
    """Stub adapter — emits no records; documents the planned shape."""

    name = "openfigi"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def __repr__(self) -> str:
        # Always mask the key — debug logging or pickling must not leak it.
        masked = "***" if self.api_key else "None"
        return f"OpenFIGIAdapter(api_key={masked})"

    async def fetch(self) -> Iterable[RefInstrument]:  # pragma: no cover - skeleton
        return []

    async def health_check(self) -> bool:  # pragma: no cover - skeleton
        return self.api_key is not None


__all__ = ["OpenFIGIAdapter"]
