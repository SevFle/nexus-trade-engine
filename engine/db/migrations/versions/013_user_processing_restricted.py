"""add processing_restricted flag to users (gh#157)

Revision ID: 013_user_processing_restricted
Revises: 012_dsr_requests
Create Date: 2026-06-25

GDPR Art. 18 restriction flag. The SQLAlchemy ``User`` model and
``engine/privacy/deletion.py`` already reference ``users.processing_restricted``,
but no migration ever added the column, so ``alembic upgrade head`` produced a
schema missing it and every test touching ``users`` failed with
``asyncpg.exceptions.UndefinedColumnError``.

Mirrors the model definition exactly:
    processing_restricted: Mapped[bool] = mapped_column(
        default=False, server_default="false"
    )
i.e. NOT NULL boolean with a ``false`` server default (so pre-existing rows get
a value). Follows the same pattern as ``mfa_enabled`` (009_user_mfa_columns).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "013_user_processing_restricted"
down_revision: str | Sequence[str] | None = "012_dsr_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "processing_restricted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "processing_restricted")
