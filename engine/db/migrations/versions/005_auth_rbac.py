"""Add auth/RBAC columns to users and create refresh_tokens table

Revision ID: 004_auth_rbac
Revises: 003_bt_result_nullable_pid
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "005_auth_rbac"
down_revision: str | Sequence[str] | None = "004_legal_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(20), server_default="user", nullable=False))
    op.add_column(
        "users", sa.Column("auth_provider", sa.String(20), server_default="local", nullable=False)
    )
    op.add_column(
        "users",
        sa.Column("external_id", sa.String(255), nullable=True),
    )
    op.alter_column("users", "hashed_password", existing_type=sa.String(255), nullable=True)

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_provider_external "
        "ON users (auth_provider, external_id) WHERE external_id IS NOT NULL"
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")

    op.execute("DROP INDEX IF EXISTS uq_user_provider_external")

    op.alter_column("users", "hashed_password", existing_type=sa.String(255), nullable=False)
    op.drop_column("users", "external_id")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "role")
