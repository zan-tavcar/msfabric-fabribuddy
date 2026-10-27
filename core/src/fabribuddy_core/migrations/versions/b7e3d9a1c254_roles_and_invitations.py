"""Roles (admin/user) and invitations

Revision ID: b7e3d9a1c254
Revises: 8a4f2c1d7e90
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e3d9a1c254"
down_revision = "8a4f2c1d7e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every account so far registered its own organisation: it administers it.
    op.execute("UPDATE user_account SET role = 'admin' WHERE role = 'owner'")
    op.create_table(
        "invitation",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column(
            "invited_by",
            sa.String(36),
            sa.ForeignKey("user_account.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_invitation_customer_id", "invitation", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_invitation_customer_id", "invitation")
    op.drop_table("invitation")
    op.execute("UPDATE user_account SET role = 'owner' WHERE role = 'admin'")
