"""add MFA columns to users (gh#126)

Revision ID: 009_user_mfa_columns
Revises: 008_evaluator_score_columns
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "009_user_mfa_columns"
down_revision: str | Sequence[str] | None = "008_evaluator_score_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "mfa_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("mfa_secret_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("mfa_backup_codes", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "mfa_backup_codes")
    op.drop_column("users", "mfa_secret_encrypted")
    op.drop_column("users", "mfa_enabled")
