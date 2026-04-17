"""add tax_lot_records table

Revision ID: 003_tax_lots
Revises: 003_bt_result_nullable_pid
Create Date: 2026-04-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "003_tax_lots"
down_revision: str | Sequence[str] | None = "003_bt_result_nullable_pid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tax_lot_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lot_id",
            sa.String(36),
            unique=True,
            nullable=False,
        ),
        sa.Column(
            "portfolio_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("symbol", sa.String(20), nullable=False, index=True),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("purchase_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("purchase_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cost_basis_adjustment",
            sa.Numeric(18, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_tax_lot_portfolio_symbol",
        "tax_lot_records",
        ["portfolio_id", "symbol"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_tax_lot_portfolio_symbol", table_name="tax_lot_records", if_exists=True)
    op.drop_table("tax_lot_records", if_exists=True)
