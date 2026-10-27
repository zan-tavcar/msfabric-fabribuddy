"""Accounts that sign in with Microsoft

Revision ID: c4d9a5f2b163
Revises: b3c8f4e1a052
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d9a5f2b163"
down_revision = "b3c8f4e1a052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_account") as batch:
        batch.add_column(sa.Column("ms_oid", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("ms_tid", sa.String(64), nullable=False, server_default=""))
        batch.create_index("ix_user_account_ms_oid", ["ms_oid"])


def downgrade() -> None:
    with op.batch_alter_table("user_account") as batch:
        batch.drop_index("ix_user_account_ms_oid")
        batch.drop_column("ms_tid")
        batch.drop_column("ms_oid")
