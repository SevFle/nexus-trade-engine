from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl


@dataclass
class MarketState:
    """Current market snapshot exposed to strategies. Stub for SEV-277."""

    symbol: str
    timestamp: str
    bars: pl.DataFrame | None = None

    def latest_close(self) -> float:
        raise NotImplementedError
