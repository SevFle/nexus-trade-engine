"""add composite_score and score_breakdown columns to backtest_results

Revision ID: 008_evaluator_score_columns
Revises: 007_scoring_snapshots
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "008_evaluator_score_columns"
down_revision: str | Sequence[str] | None = "007_scoring_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backtest_results",
        sa.Column("composite_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "backtest_results",
        sa.Column("score_breakdown", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_results", "score_breakdown")
    op.drop_column("backtest_results", "composite_score")
