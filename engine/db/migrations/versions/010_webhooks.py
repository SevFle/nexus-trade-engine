"""add webhook_configs and webhook_deliveries tables (gh#80)

Revision ID: 010_webhooks
Revises: 009_user_mfa_columns
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "010_webhooks"
down_revision: str | Sequence[str] | None = "009_user_mfa_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_configs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "portfolio_id",
            sa.UUID(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column(
            "event_types",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("signing_secret", sa.String(128), nullable=False),
        sa.Column(
            "custom_headers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "template", sa.String(20), nullable=False, server_default=sa.text("'generic'")
        ),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        "ix_webhook_configs_user_active",
        "webhook_configs",
        ["user_id", "is_active"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "webhook_id",
            sa.UUID(),
            sa.ForeignKey("webhook_configs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_webhook_deliveries_webhook_status",
        "webhook_deliveries",
        ["webhook_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_webhook_status", "webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_configs_user_active", "webhook_configs")
    op.drop_table("webhook_configs")
