"""
Slippage models for paper trading execution.

Each model computes a per-share slippage amount given order context.
Models are pluggable so the PaperBackend can be configured independently
of the BacktestBackend's fixed bps approach.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from engine.core.cost_model import CostBreakdown


class SlippageModelType(StrEnum):
    FIXED_BPS = "fixed_bps"
    PERCENTAGE = "percentage"
    SQUARE_ROOT = "square_root"
    VOLUME_WEIGHTED = "volume_weighted"
    RANDOM_WALK = "random_walk"


@dataclass
class SlippageContext:
    symbol: str
    side: str
    quantity: int
    market_price: float
    avg_volume: int = 0
    costs: CostBreakdown | None = None


class SlippageModel(ABC):
    @abstractmethod
    def compute(self, ctx: SlippageContext) -> float:
        """Return per-share slippage amount (always >= 0)."""
        ...


class FixedBpsSlippage(SlippageModel):
    def __init__(self, bps: float = 5.0) -> None:
        self.bps = bps

    def compute(self, ctx: SlippageContext) -> float:
        return ctx.market_price * (self.bps / 10_000)


class PercentageSlippage(SlippageModel):
    def __init__(self, pct: float = 0.0005) -> None:
        self.pct = pct

    def compute(self, ctx: SlippageContext) -> float:
        return ctx.market_price * self.pct


class SquareRootSlippage(SlippageModel):
    def __init__(self, base_bps: float = 5.0, volume_scale: float = 0.1) -> None:
        self.base_bps = base_bps
        self.volume_scale = volume_scale

    def compute(self, ctx: SlippageContext) -> float:
        base = ctx.market_price * (self.base_bps / 10_000)
        if ctx.avg_volume > 0 and ctx.quantity > 0:
            participation = ctx.quantity / ctx.avg_volume
            impact = self.volume_scale * math.sqrt(participation)
            return base * (1.0 + impact)
        return base


class VolumeWeightedSlippage(SlippageModel):
    def __init__(self, base_bps: float = 5.0, max_impact_bps: float = 50.0) -> None:
        self.base_bps = base_bps
        self.max_impact_bps = max_impact_bps

    def compute(self, ctx: SlippageContext) -> float:
        base = ctx.market_price * (self.base_bps / 10_000)
        if ctx.avg_volume > 0 and ctx.quantity > 0:
            participation = ctx.quantity / ctx.avg_volume
            impact_bps = min(participation * 100, self.max_impact_bps)
            impact = ctx.market_price * (impact_bps / 10_000)
            return base + impact
        return base


class RandomWalkSlippage(SlippageModel):
    def __init__(
        self,
        base_bps: float = 5.0,
        volatility_factor: float = 0.5,
        rng: object | None = None,
    ) -> None:
        import random

        self.base_bps = base_bps
        self.volatility_factor = volatility_factor
        self._rng = rng if rng is not None else random.Random()

    def compute(self, ctx: SlippageContext) -> float:
        base = ctx.market_price * (self.base_bps / 10_000)
        jitter = base * self.volatility_factor * self._rng.gauss(0, 1)
        return max(0.0, base + jitter)


SLIPPAGE_MODEL_REGISTRY: dict[SlippageModelType, type[SlippageModel]] = {
    SlippageModelType.FIXED_BPS: FixedBpsSlippage,
    SlippageModelType.PERCENTAGE: PercentageSlippage,
    SlippageModelType.SQUARE_ROOT: SquareRootSlippage,
    SlippageModelType.VOLUME_WEIGHTED: VolumeWeightedSlippage,
    SlippageModelType.RANDOM_WALK: RandomWalkSlippage,
}


def create_slippage_model(
    model_type: SlippageModelType | str,
    **kwargs: object,
) -> SlippageModel:
    if isinstance(model_type, str):
        model_type = SlippageModelType(model_type)
    cls = SLIPPAGE_MODEL_REGISTRY[model_type]
    return cls(**kwargs)
