"""
Pluggable commission calculator for paper trading.

Provides multiple commission models: per-share, flat-rate,
percentage-based, tiered, and zero-commission. All models
implement the ICommissionCalculator protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class CommissionModelType(StrEnum):
    PER_SHARE = "per_share"
    FLAT_RATE = "flat_rate"
    PERCENTAGE = "percentage"
    TIERED = "tiered"
    ZERO = "zero"


@dataclass
class CommissionQuote:
    estimated_commission: float
    exchange_fee: float
    regulatory_fee: float
    total: float


class ICommissionCalculator(ABC):
    @abstractmethod
    def calculate(self, quantity: int, price: float, side: str) -> CommissionQuote:
        ...


class PerShareCommission(ICommissionCalculator):
    def __init__(
        self,
        rate_per_share: float = 0.005,
        min_commission: float = 1.0,
        exchange_fee_per_share: float = 0.001,
        regulatory_fee_rate: float = 0.0000221,
    ) -> None:
        self._rate = rate_per_share
        self._min = min_commission
        self._exchange_fee = exchange_fee_per_share
        self._regulatory_rate = regulatory_fee_rate

    def calculate(self, quantity: int, _price: float, side: str) -> CommissionQuote:
        commission = max(self._rate * quantity, self._min)
        exchange_fee = self._exchange_fee * quantity
        regulatory_fee = self._regulatory_rate * quantity if side == "sell" else 0.0
        return CommissionQuote(
            estimated_commission=round(commission, 4),
            exchange_fee=round(exchange_fee, 4),
            regulatory_fee=round(regulatory_fee, 6),
            total=round(commission + exchange_fee + regulatory_fee, 4),
        )


class FlatRateCommission(ICommissionCalculator):
    def __init__(
        self,
        flat_rate: float = 4.95,
        exchange_fee: float = 0.0,
    ) -> None:
        self._rate = flat_rate
        self._exchange_fee = exchange_fee

    def calculate(self, _quantity: int, _price: float, _side: str) -> CommissionQuote:
        return CommissionQuote(
            estimated_commission=self._rate,
            exchange_fee=self._exchange_fee,
            regulatory_fee=0.0,
            total=round(self._rate + self._exchange_fee, 4),
        )


class PercentageCommission(ICommissionCalculator):
    def __init__(
        self,
        rate_pct: float = 0.001,
        min_commission: float = 1.0,
        regulatory_fee_rate: float = 0.0000221,
    ) -> None:
        self._rate = rate_pct
        self._min = min_commission
        self._regulatory_rate = regulatory_fee_rate

    def calculate(self, quantity: int, price: float, side: str) -> CommissionQuote:
        notional = quantity * price
        commission = max(notional * self._rate, self._min)
        regulatory_fee = self._regulatory_rate * quantity if side == "sell" else 0.0
        return CommissionQuote(
            estimated_commission=round(commission, 4),
            exchange_fee=0.0,
            regulatory_fee=round(regulatory_fee, 6),
            total=round(commission + regulatory_fee, 4),
        )


class TieredCommission(ICommissionCalculator):
    def __init__(
        self,
        tiers: list[tuple[int, float]] | None = None,
        min_commission: float = 1.0,
    ) -> None:
        self._tiers = tiers or [
            (0, 0.008),
            (500, 0.005),
            (2000, 0.003),
            (10000, 0.001),
        ]
        self._min = min_commission

    def calculate(self, quantity: int, _price: float, _side: str) -> CommissionQuote:
        rate = self._tiers[-1][1]
        for threshold, tier_rate in self._tiers:
            if quantity >= threshold:
                rate = tier_rate
        commission = max(rate * quantity, self._min)
        return CommissionQuote(
            estimated_commission=round(commission, 4),
            exchange_fee=0.0,
            regulatory_fee=0.0,
            total=round(commission, 4),
        )


class ZeroCommission(ICommissionCalculator):
    def calculate(self, _quantity: int, _price: float, _side: str) -> CommissionQuote:
        return CommissionQuote(
            estimated_commission=0.0,
            exchange_fee=0.0,
            regulatory_fee=0.0,
            total=0.0,
        )


_COMMISSION_REGISTRY: dict[CommissionModelType, type[ICommissionCalculator]] = {
    CommissionModelType.PER_SHARE: PerShareCommission,
    CommissionModelType.FLAT_RATE: FlatRateCommission,
    CommissionModelType.PERCENTAGE: PercentageCommission,
    CommissionModelType.TIERED: TieredCommission,
    CommissionModelType.ZERO: ZeroCommission,
}


def create_commission_calculator(
    model_type: CommissionModelType | str = CommissionModelType.PER_SHARE,
    **kwargs: object,
) -> ICommissionCalculator:
    if isinstance(model_type, str):
        model_type = CommissionModelType(model_type)
    cls = _COMMISSION_REGISTRY[model_type]
    return cls(**kwargs)
