"""Three roles (admin, contributor, viewer); exact-row flag on Delta stats

Revision ID: c5f2a9d1e730
Revises: b4e8f1a2c605
Create Date: 2026-10-02

The read-only role was called "user"; it becomes "viewer", and "contributor"
(may write table and column descriptions) sits between it and admin.
"""

import sqlalchemy as sa
from alembic import op

revision = "c5f2a9d1e730"
down_revision = "b4e8f1a2c605"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE user_account SET role = 'viewer' WHERE role = 'user'")
    op.execute("UPDATE invitation SET role = 'viewer' WHERE role = 'user'")
    with op.batch_alter_table("delta_stats") as batch:
        batch.add_column(sa.Column("rows_exact", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("delta_stats") as batch:
        batch.drop_column("rows_exact")
    op.execute("UPDATE user_account SET role = 'user' WHERE role IN ('viewer', 'contributor')")
    op.execute("UPDATE invitation SET role = 'user' WHERE role IN ('viewer', 'contributor')")
