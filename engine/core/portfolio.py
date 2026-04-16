from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortfolioState:
    """In-memory portfolio state during backtest execution. Stub for SEV-276."""

    cash: float = 100_000.0
    positions: dict[str, float] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.positions

    def apply_fill(self, symbol: str, quantity: float, price: float) -> None:
        raise NotImplementedError
