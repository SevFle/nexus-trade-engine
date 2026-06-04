"""normalize legacy role aliases (viewer -> user, quant_dev -> developer)

Revision ID: 002_normalize_legacy_roles
Revises: 001_tax_lots
Create Date: 2026-06-04 13:30:00.000000

One-time audit/migration that rewrites any user.role value that was set via
the old role aliases ("viewer", "quant_dev") to their canonical equivalent.
The Python-side ``IAuthProvider.map_roles`` rewrites these on the fly for
newly-mapped claims, but rows that pre-date the promotion table (or were
hand-edited by an operator) could still carry the legacy literal — which
breaks role-hierarchy comparisons elsewhere in the codebase.
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "002_normalize_legacy_roles"
down_revision: str | None = "001_tax_lots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_TO_CANONICAL: dict[str, str] = {
    "viewer": "user",
    "quant_dev": "developer",
}


def upgrade() -> None:
    bind = op.get_bind()
    for legacy, canonical in _LEGACY_TO_CANONICAL.items():
        bind.execute(
            text(
                "UPDATE users SET role = :canonical "
                "WHERE role = :legacy"
            ),
            {"canonical": canonical, "legacy": legacy},
        )


def downgrade() -> None:
    # Downgrade is intentionally a no-op: we cannot recover the original
    # legacy alias from a canonical role, and reverting would risk silently
    # downgrading accounts that were genuinely promoted.
    pass
