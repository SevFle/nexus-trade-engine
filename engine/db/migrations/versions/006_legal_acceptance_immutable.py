"""add immutability trigger on legal_acceptances

Revision ID: 005_legal_acceptance_immutable
Revises: 004_legal_documents
Create Date: 2026-04-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "006_legal_acceptance_immutable"
down_revision: str | Sequence[str] | None = "005_auth_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_acceptance_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'legal_acceptances records are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS no_acceptance_update ON legal_acceptances")
    op.execute(
        """
        CREATE TRIGGER no_acceptance_update
        BEFORE UPDATE OR DELETE ON legal_acceptances
        FOR EACH ROW EXECUTE FUNCTION prevent_acceptance_modification()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS no_acceptance_update ON legal_acceptances")
    op.execute("DROP FUNCTION IF EXISTS prevent_acceptance_modification()")
