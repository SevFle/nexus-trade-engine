from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BacktestConfig:
    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000.0


class BacktestRunner:
    """Stub for SEV-279 — orchestrates backtest execution."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    async def run(self) -> dict:
        raise NotImplementedError
