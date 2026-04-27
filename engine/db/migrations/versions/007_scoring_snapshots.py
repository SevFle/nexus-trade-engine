"""add scoring_snapshots table

Revision ID: 007_scoring_snapshots
Revises: 006_legal_acceptance_immutable
Create Date: 2026-04-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "007_scoring_snapshots"
down_revision: str | Sequence[str] | None = "006_legal_acceptance_immutable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scoring_snapshots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("universe_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_factors", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("results", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_scoring_snapshot_strategy_time",
        "scoring_snapshots",
        ["strategy_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scoring_snapshot_strategy_time")
    op.drop_table("scoring_snapshots")
