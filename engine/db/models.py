"""
Database models — persistent state for the trading engine.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Float, Integer, Boolean, DateTime, JSON, ForeignKey, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


def utcnow():
    return datetime.now(timezone.utc)


# ── Users & Auth ──

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    portfolios: Mapped[list["PortfolioRecord"]] = relationship(back_populates="user")


# ── Portfolios ──

class PortfolioRecord(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(20), default="paper")  # backtest | paper | live
    initial_cash: Mapped[float] = mapped_column(Float, default=100_000.0)
    current_cash: Mapped[float] = mapped_column(Float, default=100_000.0)
    total_value: Mapped[float] = mapped_column(Float, default=100_000.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="portfolios")
    positions: Mapped[list["PositionRecord"]] = relationship(back_populates="portfolio")
    orders: Mapped[list["OrderRecord"]] = relationship(back_populates="portfolio")


# ── Positions ──

class PositionRecord(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    portfolio: Mapped["PortfolioRecord"] = relationship(back_populates="positions")

    __table_args__ = (
        Index("ix_positions_portfolio_symbol", "portfolio_id", "symbol", unique=True),
    )


# ── Orders ──

class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(100), index=True)
    signal_id: Mapped[str] = mapped_column(String(36))

    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10))  # buy | sell
    quantity: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(20), default="market")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)

    # Cost breakdown (stored as JSON)
    cost_breakdown: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Fill info
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fill_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    portfolio: Mapped["PortfolioRecord"] = relationship(back_populates="orders")


# ── Installed Strategies ──

class InstalledStrategy(Base):
    __tablename__ = "installed_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(20))
    author: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ── Backtest Results ──

class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")

    # Config
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    start_date: Mapped[str] = mapped_column(String(20))
    end_date: Mapped[str] = mapped_column(String(20))
    initial_cash: Mapped[float] = mapped_column(Float)
    symbols: Mapped[list] = mapped_column(JSON, default=list)

    # Results
    final_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sortino_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    total_costs: Mapped[float] = mapped_column(Float, default=0.0)
    total_taxes: Mapped[float] = mapped_column(Float, default=0.0)

    # Full equity curve + trade log stored as JSON
    equity_curve: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    trade_log: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)


# ── Market Data Cache (TimescaleDB hypertable) ──

class OHLCVBar(Base):
    __tablename__ = "ohlcv_bars"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval: Mapped[str] = mapped_column(String(10), default="1d")  # 1m, 5m, 1h, 1d
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_ohlcv_symbol_ts", "symbol", "timestamp"),
    )
