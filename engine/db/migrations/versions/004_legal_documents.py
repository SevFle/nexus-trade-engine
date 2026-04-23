"""add legal_documents, legal_acceptances, data_provider_attributions

Revision ID: 004_legal_documents
Revises: 003_bt_result_nullable_pid
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "004_legal_documents"
down_revision: str | Sequence[str] | None = "003_bt_result_nullable_pid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legal_documents",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(50), unique=True, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("current_version", sa.String(20), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column(
            "requires_acceptance", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("category", sa.String(30), nullable=False, server_default="general"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_legal_documents_category", "legal_documents", ["category"])

    op.create_table(
        "legal_acceptances",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("document_slug", sa.String(50), nullable=False),
        sa.Column("document_version", sa.String(20), nullable=False),
        sa.Column(
            "accepted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ip_address", sa.String(45), nullable=False),
        sa.Column("user_agent", sa.String(500), nullable=False),
        sa.Column("context", sa.String(50), nullable=False, server_default="onboarding"),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_acceptance_user_doc", "legal_acceptances", ["user_id", "document_slug"])
    op.create_index(
        "ix_acceptance_user_doc_ver",
        "legal_acceptances",
        ["user_id", "document_slug", "document_version"],
    )
    op.create_index("ix_acceptance_time", "legal_acceptances", ["accepted_at"])

    op.create_table(
        "data_provider_attributions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_slug", sa.String(50), unique=True, nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("attribution_text", sa.Text(), nullable=False),
        sa.Column("attribution_url", sa.String(500), nullable=True),
        sa.Column("logo_path", sa.String(255), nullable=True),
        sa.Column(
            "display_contexts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("data_provider_attributions")
    op.drop_table("legal_acceptances")
    op.drop_table("legal_documents")
