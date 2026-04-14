"""
Signal types for the SDK.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class SignalStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    symbol: str
    side: Side
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    quantity: Optional[int] = None
    strategy_id: str = ""
    strength: SignalStrength = SignalStrength.MODERATE
    reason: str = ""
    metadata: dict = Field(default_factory=dict)
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    max_cost_pct: Optional[float] = None

    @classmethod
    def buy(cls, symbol: str, strategy_id: str = "", **kwargs) -> Signal:
        return cls(symbol=symbol, side=Side.BUY, strategy_id=strategy_id, **kwargs)

    @classmethod
    def sell(cls, symbol: str, strategy_id: str = "", **kwargs) -> Signal:
        return cls(symbol=symbol, side=Side.SELL, strategy_id=strategy_id, **kwargs)

    @classmethod
    def hold(cls, symbol: str, strategy_id: str = "", **kwargs) -> Signal:
        return cls(symbol=symbol, side=Side.HOLD, strategy_id=strategy_id, **kwargs)
