"""
Risk Engine — pre-trade validation, position limits, circuit breakers.

The engine has final authority over all trades. Even if a strategy emits
a signal, the risk engine can veto it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from core.order_manager import Order
    from core.portfolio import Portfolio

logger = structlog.get_logger()


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str = ""
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class RiskEngine:
    """
    Pre-trade risk validation.

    Enforces portfolio-level risk rules that individual strategies
    should not be able to override.
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,
        max_portfolio_risk_pct: float = 0.25,
        max_open_positions: int = 50,
        circuit_breaker_drawdown_pct: float = 0.10,
        max_daily_trades: int = 100,
        max_single_order_value: float = 50_000.0,
    ):
        self.max_position_pct = max_position_pct
        self.max_portfolio_risk_pct = max_portfolio_risk_pct
        self.max_open_positions = max_open_positions
        self.circuit_breaker_drawdown_pct = circuit_breaker_drawdown_pct
        self.max_daily_trades = max_daily_trades
        self.max_single_order_value = max_single_order_value

        # State
        self.circuit_breaker_active = False
        self.daily_trade_count = 0

    def check_order(
        self, order: Order, portfolio: Portfolio, market_price: float
    ) -> RiskCheckResult:
        """
        Run all pre-trade risk checks.
        Returns approved=False with reason if any check fails.
        """
        warnings = []

        # Circuit breaker check
        if self.circuit_breaker_active:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"Circuit breaker active: portfolio drawdown exceeded"
                    f" {self.circuit_breaker_drawdown_pct:.0%}"
                ),
            )

        # Check portfolio drawdown
        drawdown = self._calculate_drawdown(portfolio)
        if drawdown >= self.circuit_breaker_drawdown_pct:
            self.circuit_breaker_active = True
            logger.critical("risk.circuit_breaker_triggered", drawdown=drawdown)
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"Circuit breaker triggered: drawdown {drawdown:.2%}"
                    f" >= {self.circuit_breaker_drawdown_pct:.2%}"
                ),
            )

        # Max open positions
        if (
            order.side.value == "buy"
            and order.symbol not in portfolio.positions
            and len(portfolio.positions) >= self.max_open_positions
        ):
            return RiskCheckResult(
                approved=False,
                reason=f"Max open positions reached: {self.max_open_positions}",
            )

        # Single position concentration
        order_value = order.quantity * market_price
        if portfolio.total_value > 0:
            existing_value = 0
            if order.symbol in portfolio.positions:
                existing_value = portfolio.positions[order.symbol].market_value
            new_weight = (existing_value + order_value) / portfolio.total_value
            if order.side.value == "buy" and new_weight > self.max_position_pct:
                return RiskCheckResult(
                    approved=False,
                    reason=(
                        f"Position {order.symbol} would be {new_weight:.1%}"
                        f" of portfolio (max {self.max_position_pct:.0%})"
                    ),
                )

        # Single order value cap
        if order_value > self.max_single_order_value:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"Order value ${order_value:,.0f} exceeds"
                    f" max ${self.max_single_order_value:,.0f}"
                ),
            )

        # Daily trade limit
        if self.daily_trade_count >= self.max_daily_trades:
            return RiskCheckResult(
                approved=False,
                reason=f"Daily trade limit reached: {self.max_daily_trades}",
            )

        # Warn if order is large relative to cash
        if order.side.value == "buy":
            cash_usage = order_value / portfolio.cash if portfolio.cash > 0 else float("inf")
            if cash_usage > 0.5:
                warnings.append(f"Order uses {cash_usage:.0%} of available cash")

        self.daily_trade_count += 1

        return RiskCheckResult(approved=True, warnings=warnings)

    def reset_daily_counters(self):
        """Called at start of each trading day."""
        self.daily_trade_count = 0

    def reset_circuit_breaker(self):
        """Manual reset after review."""
        self.circuit_breaker_active = False
        logger.info("risk.circuit_breaker_reset")

    def _calculate_drawdown(self, portfolio: Portfolio) -> float:
        """Current drawdown from initial capital."""
        if portfolio.initial_cash == 0:
            return 0.0
        return max(0.0, (portfolio.initial_cash - portfolio.total_value) / portfolio.initial_cash)
