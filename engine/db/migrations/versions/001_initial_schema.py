"""initial schema

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-04-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "portfolios",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("initial_capital", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_portfolios_user_id"), "portfolios", ["user_id"], unique=False)

    op.create_table(
        "positions",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("avg_entry_price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("current_price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portfolio_id", "symbol", name="uq_position_portfolio_symbol"),
    )
    op.create_index(op.f("ix_positions_portfolio_id"), "positions", ["portfolio_id"], unique=False)
    op.create_index(op.f("ix_positions_symbol"), "positions", ["symbol"], unique=False)

    op.create_table(
        "orders",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("order_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_orders_portfolio_id"), "orders", ["portfolio_id"], unique=False)
    op.create_index(op.f("ix_orders_symbol"), "orders", ["symbol"], unique=False)

    op.create_table(
        "installed_strategies",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_installed_strategies_portfolio_id"),
        "installed_strategies",
        ["portfolio_id"],
        unique=False,
    )

    op.create_table(
        "backtest_results",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_backtest_results_portfolio_id"),
        "backtest_results",
        ["portfolio_id"],
        unique=False,
    )

    op.create_table(
        "ohlcv_bars",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("volume", sa.Numeric(precision=24, scale=4), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "timestamp"),
        sa.UniqueConstraint("symbol", "timestamp", name="uq_ohlcv_symbol_timestamp"),
    )
    op.create_index(
        "ix_ohlcv_symbol_timestamp", "ohlcv_bars", ["symbol", "timestamp"], unique=False
    )

    op.execute("SELECT create_hypertable('ohlcv_bars', 'timestamp', if_not_exists => TRUE)")


def downgrade() -> None:
    op.drop_table("ohlcv_bars")
    op.drop_table("backtest_results")
    op.drop_table("installed_strategies")
    op.drop_table("orders")
    op.drop_table("positions")
    op.drop_table("portfolios")
    op.drop_table("users")
