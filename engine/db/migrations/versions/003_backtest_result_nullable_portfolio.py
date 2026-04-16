"""backtest_result_nullable_portfolio

Revision ID: 003_backtest_result_nullable_portfolio
Revises: 002_additional_tables
Create Date: 2026-04-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "003_backtest_result_nullable_portfolio"
down_revision: str | Sequence[str] | None = "002_additional_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "backtest_results",
        "portfolio_id",
        existing_type=sa.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "backtest_results",
        "portfolio_id",
        existing_type=sa.UUID(as_uuid=True),
        nullable=False,
    )
