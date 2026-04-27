"""Quality-Momentum Scoring Strategy.

Ranks a stock universe using a 5-factor composite Z-score model:
- ROE (Return on Equity) — higher is better
- P/E Ratio — lower is better
- Price Momentum (12-month return) — higher is better
- Debt/Equity Ratio — lower is better
- Earnings Growth — higher is better

Outputs ranked list with 0-100 composite scores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus_sdk.scoring import (
    FactorDirection,
    IScoringStrategy,
    ScoringFactor,
    ScoringResult,
)

if TYPE_CHECKING:
    from nexus_sdk.signals import Signal
    from nexus_sdk.strategy import MarketState, StrategyConfig


class Strategy(IScoringStrategy):
    @property
    def id(self) -> str:
        return "quality_momentum"

    @property
    def name(self) -> str:
        return "Quality-Momentum Scoring"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Multi-factor Quality-Momentum scoring using composite Z-score ranking"

    async def initialize(self, config: StrategyConfig) -> None:
        self._params = config.params

    async def dispose(self) -> None:
        pass

    async def evaluate(self, _portfolio: Any, _market: MarketState, _costs: Any) -> list[Signal]:
        return []

    def get_config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "roe_weight": {"type": "number", "default": 0.3, "minimum": 0, "maximum": 1},
                "pe_weight": {"type": "number", "default": 0.2, "minimum": 0, "maximum": 1},
                "momentum_weight": {"type": "number", "default": 0.2, "minimum": 0, "maximum": 1},
                "debt_equity_weight": {
                    "type": "number",
                    "default": 0.15,
                    "minimum": 0,
                    "maximum": 1,
                },
                "earnings_growth_weight": {
                    "type": "number",
                    "default": 0.15,
                    "minimum": 0,
                    "maximum": 1,
                },
            },
        }

    def get_scoring_factors(self) -> list[ScoringFactor]:
        return [
            ScoringFactor(
                name="roe",
                weight=self._params.get("roe_weight", 0.3),
                direction=FactorDirection.HIGHER_IS_BETTER,
            ),
            ScoringFactor(
                name="pe_ratio",
                weight=self._params.get("pe_weight", 0.2),
                direction=FactorDirection.LOWER_IS_BETTER,
                winsorize_pct=(1, 99),
            ),
            ScoringFactor(
                name="momentum_12m",
                weight=self._params.get("momentum_weight", 0.2),
                direction=FactorDirection.HIGHER_IS_BETTER,
            ),
            ScoringFactor(
                name="debt_equity",
                weight=self._params.get("debt_equity_weight", 0.15),
                direction=FactorDirection.LOWER_IS_BETTER,
            ),
            ScoringFactor(
                name="earnings_growth",
                weight=self._params.get("earnings_growth_weight", 0.15),
                direction=FactorDirection.HIGHER_IS_BETTER,
            ),
        ]

    async def score_universe(
        self, _universe: list[str], _market: MarketState, _costs: Any
    ) -> ScoringResult:
        return ScoringResult(strategy_id=self.id, scores=[])
