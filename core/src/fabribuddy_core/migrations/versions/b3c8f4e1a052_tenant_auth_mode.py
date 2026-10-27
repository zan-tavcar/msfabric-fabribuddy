"""Tenant connection by signed-in user (delegated) as well as service principal

Revision ID: b3c8f4e1a052
Revises: a2b7e3d9f041
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c8f4e1a052"
down_revision = "a2b7e3d9f041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connected_tenant") as batch:
        batch.add_column(
            sa.Column("auth_mode", sa.String(10), nullable=False, server_default="spn")
        )
        batch.add_column(
            sa.Column("signed_in_as", sa.String(200), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("connected_tenant") as batch:
        batch.drop_column("signed_in_as")
        batch.drop_column("auth_mode")
