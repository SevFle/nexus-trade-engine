"""additional_tables

Revision ID: 002_additional_tables
Revises: 001_initial_schema
Create Date: 2026-04-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "002_additional_tables"
down_revision: str | Sequence[str] | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("total_value", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True, server_default="0"),
        sa.Column("realized_pnl", sa.Float(), nullable=True, server_default="0"),
        sa.Column("num_positions", sa.Integer(), nullable=True, server_default="0"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", "timestamp"),
    )
    op.create_index(
        "ix_portfolio_snapshots_pid",
        "portfolio_snapshots",
        ["portfolio_id", sa.text("timestamp DESC")],
        unique=False,
    )
    op.execute(
        "SELECT create_hypertable('portfolio_snapshots', 'timestamp', if_not_exists => TRUE)"
    )

    op.create_table(
        "evaluation_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("strategy_id", sa.String(length=100), nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("signals_emitted", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("evaluation_ms", sa.Float(), nullable=True, server_default="0"),
        sa.Column("market_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", "timestamp"),
    )
    op.create_index(
        "ix_eval_log_strategy",
        "evaluation_log",
        ["strategy_id", sa.text("timestamp DESC")],
        unique=False,
    )
    op.execute("SELECT create_hypertable('evaluation_log', 'timestamp', if_not_exists => TRUE)")

    op.create_table(
        "tax_lots",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("cost_basis", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tax_lots_portfolio_id"), "tax_lots", ["portfolio_id"], unique=False)
    op.create_index(op.f("ix_tax_lots_symbol"), "tax_lots", ["symbol"], unique=False)

    op.create_table(
        "marketplace_entries",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "marketplace_reviews",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["entry_id"], ["marketplace_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_marketplace_reviews_entry_id"),
        "marketplace_reviews",
        ["entry_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("marketplace_reviews")
    op.drop_table("marketplace_entries")
    op.drop_table("tax_lots")
    op.drop_table("evaluation_log")
    op.drop_table("portfolio_snapshots")
