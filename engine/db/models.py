from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(default=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    auth_provider: Mapped[str] = mapped_column(String(20), default="local")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    portfolios: Mapped[list[Portfolio]] = relationship(back_populates="user")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("uq_user_provider_external", "auth_provider", "external_id", unique=True),
    )


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("100000.0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="portfolios")
    positions: Mapped[list[Position]] = relationship(back_populates="portfolio")
    orders: Mapped[list[Order]] = relationship(back_populates="portfolio")
    tax_lots: Mapped[list[TaxLotRecord]] = relationship(back_populates="portfolio")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    current_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="positions")

    __table_args__ = (
        UniqueConstraint("portfolio_id", "symbol", name="uq_position_portfolio_symbol"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10))
    order_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal]
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    portfolio: Mapped[Portfolio] = relationship(back_populates="orders")


class InstalledStrategy(Base):
    __tablename__ = "installed_strategies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    strategy_name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)  # type: ignore[assignment]
    is_active: Mapped[bool] = mapped_column(default=True)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True, nullable=True
    )
    strategy_name: Mapped[str] = mapped_column(String(100))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)  # type: ignore[assignment]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TaxLotStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_CONSUMED = "partially_consumed"
    CLOSED = "closed"


class TaxLotRecord(Base):
    __tablename__ = "tax_lot_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    lot_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    purchase_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cost_basis_adjustment: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(30), default=TaxLotStatus.OPEN.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="tax_lots")

    __table_args__ = (Index("ix_tax_lot_portfolio_symbol", "portfolio_id", "symbol"),)


class OHLCVBar(Base):
    __tablename__ = "ohlcv_bars"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(20))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4))

    __table_args__ = (
        Index("ix_ohlcv_symbol_timestamp", "symbol", "timestamp"),
        UniqueConstraint("symbol", "timestamp", name="uq_ohlcv_symbol_timestamp"),
    )


class LegalDocument(Base):
    __tablename__ = "legal_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    current_version: Mapped[str] = mapped_column(String(20))
    effective_date: Mapped[date] = mapped_column()
    requires_acceptance: Mapped[bool] = mapped_column(default=True)
    category: Mapped[str] = mapped_column(String(30), default="general", index=True)
    display_order: Mapped[int] = mapped_column(default=0)
    file_path: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class LegalAcceptance(Base):
    __tablename__ = "legal_acceptances"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"),
        index=True,
    )
    document_slug: Mapped[str] = mapped_column(String(50))
    document_version: Mapped[str] = mapped_column(String(20))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ip_address: Mapped[str] = mapped_column(String(45))
    user_agent: Mapped[str] = mapped_column(String(500))
    context: Mapped[str] = mapped_column(String(50), default="onboarding")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_acceptance_user_doc", "user_id", "document_slug"),
        Index("ix_acceptance_user_doc_ver", "user_id", "document_slug", "document_version"),
        Index("ix_acceptance_time", "accepted_at"),
    )


class DataProviderAttribution(Base):
    __tablename__ = "data_provider_attributions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider_slug: Mapped[str] = mapped_column(String(50), unique=True)
    provider_name: Mapped[str] = mapped_column(String(100))
    attribution_text: Mapped[str] = mapped_column(Text)
    attribution_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_contexts: Mapped[dict] = mapped_column(JSONB, default=list)  # type: ignore[assignment]
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")
