"""add dsr_requests table for GDPR/CCPA DSR tracking (gh#157)

Revision ID: 012_dsr_requests
Revises: 011_api_keys
Create Date: 2026-05-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "012_dsr_requests"
down_revision: str | Sequence[str] | None = "011_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dsr_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Type of request: "export" (Art. 15 / 20), "delete" (Art. 17),
        # "rectify" (Art. 16), "restrict" (Art. 18), "object" (Art. 21).
        sa.Column("kind", sa.String(32), nullable=False),
        # Lifecycle: "pending" -> "in_progress" -> ("completed" | "failed" | "cancelled")
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        # Free-form note from the user or the operator who recorded the request.
        sa.Column("note", sa.Text(), nullable=True),
        # Operator-attached metadata (e.g., evidence of identity verification,
        # outbound legal correspondence reference). JSONB for forward compat.
        sa.Column(
            "details",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # GDPR Art. 12: respond within one month. Operator can shorten.
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "ix_dsr_requests_user_kind_status",
        "dsr_requests",
        ["user_id", "kind", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_dsr_requests_user_kind_status", table_name="dsr_requests")
    op.drop_table("dsr_requests")
