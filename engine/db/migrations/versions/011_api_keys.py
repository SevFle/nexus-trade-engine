"""add api_keys table (gh#94)

Revision ID: 011_api_keys
Revises: 010_webhooks
Create Date: 2026-05-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "011_api_keys"
down_revision: str | Sequence[str] | None = "010_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        # First 12 characters of the issued token (e.g. "nxs_live_aB3").
        # Used for human-readable identification and as the lookup key.
        sa.Column("prefix", sa.String(32), nullable=False, unique=True, index=True),
        # bcrypt hash of the secret portion (the random tail after the prefix).
        sa.Column("key_hash", sa.String(255), nullable=False),
        # JSONB array of scope strings, e.g. ["read", "trade"].
        sa.Column(
            "scopes",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        "ix_api_keys_user_active",
        "api_keys",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_user_active", table_name="api_keys")
    op.drop_table("api_keys")
