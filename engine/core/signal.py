"""
Signal types — the ONLY output contract for strategy plugins.

A Signal represents an intent to trade. The engine validates, costs, and
executes signals. Strategies never create orders directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from engine.core.instruments import Instrument


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class SignalStrength(str, Enum):
    """How confident the strategy is in this signal."""
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class Signal(BaseModel):
    """
    A trading signal emitted by a strategy plugin.

    This is the ONLY type a strategy returns. The engine handles everything
    from here: cost estimation, risk checks, order creation, execution.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ── What to trade ──
    symbol: str = Field(..., description="Ticker symbol, e.g. 'AAPL'")
    # Always populated post-validation (see ``_ensure_instrument``).
    # Optional in the schema only so callers that pass a bare ``symbol``
    # don't have to construct an Instrument themselves.
    instrument: Instrument = Field(
        default=None,  # type: ignore[assignment]
        description=(
            "Typed instrument. Auto-derived from `symbol` when omitted "
            "via Instrument.from_string (defaults to equity for free-form "
            "strings; use Instrument factories for crypto/forex/options)."
        ),
        validate_default=False,
    )
    side: Side = Field(..., description="BUY, SELL, or HOLD")

    # ── How much ──
    weight: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Target allocation weight (0.0 - 1.0). Engine converts to shares.",
    )
    quantity: int | None = Field(
        default=None,
        description="Explicit share count. Overrides weight if set.",
    )

    # ── Metadata ──
    strategy_id: str = Field(..., description="ID of the emitting strategy")
    strength: SignalStrength = SignalStrength.MODERATE
    reason: str = Field(default="", description="Human-readable rationale for audit log")
    metadata: dict = Field(default_factory=dict, description="Strategy-specific data")

    # ── Risk hints (optional — engine has final say) ──
    stop_loss_pct: float | None = Field(default=None, description="Suggested stop loss %")
    take_profit_pct: float | None = Field(default=None, description="Suggested take profit %")
    max_cost_pct: float | None = Field(
        default=None,
        description="Max total cost (fees+spread+slippage) as % of trade value. Skip if exceeded.",
    )

    @model_validator(mode="after")
    def _ensure_instrument(self) -> Signal:
        """Backward-compat: auto-wrap `symbol` as an :class:`Instrument`
        when the caller didn't supply one explicitly. After this validator
        runs, ``instrument`` is guaranteed non-None (annotation reflects
        post-validation state)."""
        if self.instrument is None:  # type: ignore[unreachable]
            self.instrument = Instrument.from_string(self.symbol)
        return self

    # ── Convenience constructors ──
    @classmethod
    def buy(cls, symbol: str, strategy_id: str = "", **kwargs) -> Signal:
        return cls(symbol=symbol, side=Side.BUY, strategy_id=strategy_id, **kwargs)

    @classmethod
    def sell(cls, symbol: str, strategy_id: str = "", **kwargs) -> Signal:
        return cls(symbol=symbol, side=Side.SELL, strategy_id=strategy_id, **kwargs)

    @classmethod
    def hold(cls, symbol: str, strategy_id: str = "", **kwargs) -> Signal:
        return cls(symbol=symbol, side=Side.HOLD, strategy_id=strategy_id, **kwargs)


class SignalBatch(BaseModel):
    """A batch of signals from a single strategy evaluation cycle."""

    strategy_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    signals: list[Signal] = Field(default_factory=list)
    evaluation_time_ms: float = Field(default=0.0, description="How long evaluate() took")

    @property
    def trade_signals(self) -> list[Signal]:
        """Return only BUY/SELL signals (exclude HOLDs)."""
        return [s for s in self.signals if s.side != Side.HOLD]
