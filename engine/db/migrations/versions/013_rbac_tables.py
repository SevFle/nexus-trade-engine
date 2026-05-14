"""Add RBAC tables for roles, permissions, and user-role assignments (ADR-0002)

Revision ID: 013_rbac_tables
Revises: 012_dsr_requests
Create Date: 2026-05-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "013_rbac_tables"
down_revision: str | Sequence[str] | None = "012_dsr_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rbac_roles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(50), unique=True, nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "rbac_permissions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(50), unique=True, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "rbac_role_permissions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "role_id",
            sa.UUID(),
            sa.ForeignKey("rbac_roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            sa.UUID(),
            sa.ForeignKey("rbac_permissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_rbac_role_permissions_unique",
        "rbac_role_permissions",
        ["role_id", "permission_id"],
        unique=True,
    )

    op.create_table(
        "rbac_user_roles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role_id",
            sa.UUID(),
            sa.ForeignKey("rbac_roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_rbac_user_roles_unique",
        "rbac_user_roles",
        ["user_id", "role_id"],
        unique=True,
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("token_hash", sa.String(128), unique=True, nullable=False),
        sa.Column("token_type", sa.String(20), nullable=False, server_default="access"),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_auth_tokens_hash_active",
        "auth_tokens",
        ["token_hash", "revoked_at"],
    )

    op.execute(
        """
        INSERT INTO rbac_roles (id, name, display_name, level, description) VALUES
            (gen_random_uuid(), 'viewer', 'Viewer', 0, 'Read-only access'),
            (gen_random_uuid(), 'trader', 'Trader', 1, 'Read, write, and trade on own portfolios'),
            (gen_random_uuid(), 'admin', 'Administrator', 2, 'Full system access')
        ON CONFLICT (name) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO rbac_permissions (id, name, description) VALUES
            (gen_random_uuid(), 'read', 'Read-only access to resources'),
            (gen_random_uuid(), 'write', 'Create and modify resources'),
            (gen_random_uuid(), 'trade', 'Submit orders and manage live trading'),
            (gen_random_uuid(), 'admin', 'System administration and user management')
        ON CONFLICT (name) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO rbac_role_permissions (id, role_id, permission_id)
        SELECT gen_random_uuid(), r.id, p.id
        FROM rbac_roles r
        CROSS JOIN rbac_permissions p
        WHERE
            (r.name = 'viewer' AND p.name = 'read')
            OR (r.name = 'trader' AND p.name IN ('read', 'write', 'trade'))
            OR (r.name = 'admin')
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("auth_tokens")
    op.drop_table("rbac_user_roles")
    op.drop_table("rbac_role_permissions")
    op.drop_table("rbac_permissions")
    op.drop_table("rbac_roles")
