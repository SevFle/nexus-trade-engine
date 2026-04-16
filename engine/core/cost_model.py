"""
Cost Model — first-class citizen of the Nexus engine.

Every signal passes through the cost model before execution. Strategies
receive the cost model as input to make cost-aware decisions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum


class TaxMethod(str, Enum):
    FIFO = "fifo"
    LIFO = "lifo"
    SPECIFIC_LOT = "specific_lot"


@dataclass
class Money:
    """Monetary amount with explicit precision."""

    amount: float
    currency: str = "USD"

    def __add__(self, other: Money) -> Money:
        assert self.currency == other.currency
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        assert self.currency == other.currency
        return Money(amount=self.amount - other.amount, currency=self.currency)

    @property
    def is_zero(self) -> bool:
        return abs(self.amount) < 1e-10

    def as_pct_of(self, total: float) -> float:
        if total == 0:
            return 0.0
        return (self.amount / total) * 100


@dataclass
class CostBreakdown:
    """Itemized breakdown of all costs for a single trade."""

    commission: Money = field(default_factory=lambda: Money(0.0))
    spread: Money = field(default_factory=lambda: Money(0.0))
    slippage: Money = field(default_factory=lambda: Money(0.0))
    exchange_fee: Money = field(default_factory=lambda: Money(0.0))
    tax_estimate: Money = field(default_factory=lambda: Money(0.0))
    currency_conversion: Money = field(default_factory=lambda: Money(0.0))

    @property
    def total(self) -> Money:
        return Money(
            amount=(
                self.commission.amount
                + self.spread.amount
                + self.slippage.amount
                + self.exchange_fee.amount
                + self.tax_estimate.amount
                + self.currency_conversion.amount
            )
        )

    @property
    def total_without_tax(self) -> Money:
        return Money(amount=self.total.amount - self.tax_estimate.amount)

    def as_dict(self) -> dict:
        return {
            "commission": self.commission.amount,
            "spread": self.spread.amount,
            "slippage": self.slippage.amount,
            "exchange_fee": self.exchange_fee.amount,
            "tax_estimate": self.tax_estimate.amount,
            "currency_conversion": self.currency_conversion.amount,
            "total": self.total.amount,
        }


@dataclass
class TaxLot:
    """A single purchase lot for tax tracking."""

    symbol: str
    quantity: int
    purchase_price: float
    purchase_date: datetime
    lot_id: str = ""

    @property
    def is_long_term(self) -> bool:
        held_days = (datetime.now(UTC) - self.purchase_date).days
        return held_days >= 365

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.purchase_price


class ICostModel(ABC):
    """
    Abstract cost model interface.

    This is passed to every strategy's evaluate() call so strategies
    can factor costs into their decision-making.
    """

    # ── Per-trade cost estimation ──

    @abstractmethod
    def estimate_commission(self, symbol: str, quantity: int, price: float) -> Money:
        """Estimate broker commission for a trade."""
        ...

    @abstractmethod
    def estimate_spread(self, symbol: str, price: float, side: str) -> Money:
        """Estimate bid-ask spread cost."""
        ...

    @abstractmethod
    def estimate_slippage(
        self, symbol: str, quantity: int, price: float, avg_volume: int
    ) -> Money:
        """Estimate market impact / slippage based on order size vs volume."""
        ...

    @abstractmethod
    def estimate_total(
        self, symbol: str, quantity: int, price: float, side: str, avg_volume: int = 0
    ) -> CostBreakdown:
        """Full cost breakdown for a proposed trade."""
        ...

    @abstractmethod
    def estimate_pct(self, symbol: str, price: float, side: str = "buy") -> float:
        """Quick estimate: total round-trip cost as % of trade value."""
        ...

    # ── Tax engine ──

    @abstractmethod
    def estimate_tax(
        self,
        symbol: str,
        sell_price: float,
        quantity: int,
        lots: list[TaxLot],
        method: TaxMethod = TaxMethod.FIFO,
    ) -> Money:
        """Estimate capital gains tax for selling from given lots."""
        ...

    @abstractmethod
    def check_wash_sale(
        self,
        symbol: str,
        sell_date: datetime,
        buy_history: list[dict],
    ) -> bool:
        """Check if a sale would trigger wash sale rules (30-day window)."""
        ...

    @abstractmethod
    def estimate_dividend_tax(self, dividend_amount: float, is_qualified: bool) -> Money:
        """Tax on dividend income."""
        ...


class DefaultCostModel(ICostModel):
    """
    Default cost model implementation with configurable parameters.
    Suitable for US equities. Override for other asset classes.
    """

    def __init__(
        self,
        commission_per_trade: float = 0.0,
        spread_bps: float = 5.0,
        slippage_bps: float = 10.0,
        exchange_fee_per_share: float = 0.0003,
        short_term_tax_rate: float = 0.37,
        long_term_tax_rate: float = 0.20,
        qualified_dividend_rate: float = 0.15,
        ordinary_dividend_rate: float = 0.37,
        wash_sale_window_days: int = 30,
    ):
        self.commission_per_trade = commission_per_trade
        self.spread_bps = spread_bps
        self.slippage_bps = slippage_bps
        self.exchange_fee_per_share = exchange_fee_per_share
        self.short_term_tax_rate = short_term_tax_rate
        self.long_term_tax_rate = long_term_tax_rate
        self.qualified_dividend_rate = qualified_dividend_rate
        self.ordinary_dividend_rate = ordinary_dividend_rate
        self.wash_sale_window_days = wash_sale_window_days

    def estimate_commission(self, symbol: str, quantity: int, price: float) -> Money:
        return Money(amount=self.commission_per_trade)

    def estimate_spread(self, symbol: str, price: float, side: str) -> Money:
        spread_cost = price * (self.spread_bps / 10_000)
        return Money(amount=spread_cost)

    def estimate_slippage(
        self, symbol: str, quantity: int, price: float, avg_volume: int
    ) -> Money:
        base_slippage = price * (self.slippage_bps / 10_000) * quantity
        # Scale slippage with order size relative to volume
        if avg_volume > 0:
            participation_rate = quantity / avg_volume
            impact_multiplier = 1.0 + (participation_rate * 10)  # Linear impact model
            return Money(amount=base_slippage * impact_multiplier)
        return Money(amount=base_slippage)

    def estimate_total(
        self, symbol: str, quantity: int, price: float, side: str, avg_volume: int = 0
    ) -> CostBreakdown:
        return CostBreakdown(
            commission=self.estimate_commission(symbol, quantity, price),
            spread=self.estimate_spread(symbol, price, side),
            slippage=self.estimate_slippage(symbol, quantity, price, avg_volume),
            exchange_fee=Money(amount=self.exchange_fee_per_share * quantity),
        )

    def estimate_pct(self, symbol: str, price: float, side: str = "buy") -> float:
        """Round-trip cost estimate as percentage of trade value."""
        one_side_bps = self.spread_bps + self.slippage_bps
        round_trip_bps = one_side_bps * 2
        commission_bps = (self.commission_per_trade / price) * 10_000 if price > 0 else 0
        return (round_trip_bps + commission_bps) / 10_000  # Convert to decimal pct

    def estimate_tax(
        self,
        symbol: str,
        sell_price: float,
        quantity: int,
        lots: list[TaxLot],
        method: TaxMethod = TaxMethod.FIFO,
    ) -> Money:
        if method == TaxMethod.FIFO:
            sorted_lots = sorted(lots, key=lambda l: l.purchase_date)
        elif method == TaxMethod.LIFO:
            sorted_lots = sorted(lots, key=lambda l: l.purchase_date, reverse=True)
        else:
            sorted_lots = lots

        remaining = quantity
        total_tax = 0.0

        for lot in sorted_lots:
            if remaining <= 0:
                break
            shares_from_lot = min(remaining, lot.quantity)
            gain = (sell_price - lot.purchase_price) * shares_from_lot

            if gain > 0:
                rate = self.long_term_tax_rate if lot.is_long_term else self.short_term_tax_rate
                total_tax += gain * rate

            remaining -= shares_from_lot

        return Money(amount=max(0.0, total_tax))

    def check_wash_sale(
        self,
        symbol: str,
        sell_date: datetime,
        buy_history: list[dict],
    ) -> bool:
        window_start = sell_date - timedelta(days=self.wash_sale_window_days)
        window_end = sell_date + timedelta(days=self.wash_sale_window_days)

        for buy in buy_history:
            buy_date = buy.get("date")
            buy_symbol = buy.get("symbol", "")
            if buy_symbol == symbol and window_start <= buy_date <= window_end:
                return True
        return False

    def calculate_wash_sale_adjustment(
        self,
        symbol: str,
        sell_date: datetime,
        loss: float,
        buy_history: list[dict],
    ) -> float:
        if loss >= 0:
            return 0.0
        if not self.check_wash_sale(symbol, sell_date, buy_history):
            return 0.0
        return abs(loss)

    def estimate_dividend_tax(self, dividend_amount: float, is_qualified: bool) -> Money:
        rate = self.qualified_dividend_rate if is_qualified else self.ordinary_dividend_rate
        return Money(amount=dividend_amount * rate)
