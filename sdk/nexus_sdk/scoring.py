"""
Multi-Factor Z-Score Scoring Engine SDK.

Provides the IScoringStrategy interface, scoring models, and ZScoreNormalizer
for building universe-wide ranking strategies.
"""

from __future__ import annotations

from abc import abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from nexus_sdk.strategy import IStrategy, MarketState


class FactorDirection(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class ScoringFactor(BaseModel):
    name: str
    weight: float = Field(ge=0.0, le=1.0)
    direction: FactorDirection = FactorDirection.HIGHER_IS_BETTER
    composite_fields: list[str] = Field(default_factory=list)
    winsorize_pct: tuple[float, float] = Field(default=(1.0, 99.0))


class FactorScore(BaseModel):
    factor_name: str
    z_score: float = 0.0
    raw_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_name": self.factor_name,
            "z_score": self.z_score,
            "raw_value": self.raw_value,
        }


class SymbolScore(BaseModel):
    symbol: str
    composite_score: float = Field(default=0.0, ge=0.0, le=100.0)
    rank: int = Field(default=0, ge=0)
    factor_scores: dict[str, FactorScore] = Field(default_factory=dict)

    @field_validator("composite_score", mode="before")
    @classmethod
    def clamp_score(cls, v: float) -> float:
        return max(0.0, min(100.0, v))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "composite_score": self.composite_score,
            "rank": self.rank,
            "factor_scores": {k: v.to_dict() for k, v in self.factor_scores.items()},
        }


class ScoringResult(BaseModel):
    strategy_id: str
    scores: list[SymbolScore] = Field(default_factory=list)
    excluded_factors: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        self.scores = sorted(self.scores, key=lambda s: s.composite_score, reverse=True)
        for i, score in enumerate(self.scores, start=1):
            score.rank = i

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "scores": [s.to_dict() for s in self.scores],
            "excluded_factors": self.excluded_factors,
        }


class ZScoreNormalizer:
    """Stateless utility: winsorize → standardize → scale."""

    def __init__(self, winsorize_lower: float = 1.0, winsorize_upper: float = 99.0) -> None:
        self.winsorize_lower = winsorize_lower
        self.winsorize_upper = winsorize_upper

    def winsorize(self, values: list[float | None]) -> list[float]:
        if not values:
            return []
        clean = [v for v in values if v is not None]
        if not clean:
            return []
        if len(clean) == 1:
            return [clean[0]]
        clean_sorted = sorted(clean)
        n = len(clean_sorted)
        lo_idx = max(0, int(n * self.winsorize_lower / 100.0))
        hi_idx = min(n - 1, int(n * self.winsorize_upper / 100.0))
        lo_val = clean_sorted[lo_idx]
        hi_val = clean_sorted[hi_idx]
        return [max(lo_val, min(hi_val, v)) for v in clean]

    def standardize(self, values: list[float]) -> list[float]:
        if not values:
            return []
        if len(values) == 1:
            return [0.0]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        if variance == 0:
            return [0.0] * n
        std = variance**0.5
        return [(v - mean) / std for v in values]

    def scale_to_range(
        self, values: list[float], low: float = 0.0, high: float = 100.0
    ) -> list[float]:
        if not values:
            return []
        if len(values) == 1:
            return [(low + high) / 2.0]
        v_min = min(values)
        v_max = max(values)
        if v_min == v_max:
            return [(low + high) / 2.0] * len(values)
        return [low + (v - v_min) / (v_max - v_min) * (high - low) for v in values]

    def winsorize_and_standardize(
        self,
        values: list[float | None],
        winsorize_lower: float | None = None,
        winsorize_upper: float | None = None,
    ) -> list[float]:
        temp = ZScoreNormalizer(
            winsorize_lower=winsorize_lower
            if winsorize_lower is not None
            else self.winsorize_lower,
            winsorize_upper=winsorize_upper
            if winsorize_upper is not None
            else self.winsorize_upper,
        )
        winsorized = temp.winsorize(values)
        return temp.standardize(winsorized)


class IScoringStrategy(IStrategy):
    """Strategy plugin interface for universe-wide scoring."""

    @abstractmethod
    def get_scoring_factors(self) -> list[ScoringFactor]:
        """Return the list of scoring factors with weights and directions."""
        ...

    @abstractmethod
    async def score_universe(
        self, universe: list[str], market: MarketState, costs: Any
    ) -> ScoringResult:
        """Score and rank an entire stock universe."""
        ...
