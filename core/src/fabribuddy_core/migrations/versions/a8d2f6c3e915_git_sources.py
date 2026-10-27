"""Git source mode: where a workspace's definitions are read from

Revision ID: a8d2f6c3e915
Revises: f3a9c1e7d402
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "a8d2f6c3e915"
down_revision = "f3a9c1e7d402"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "git_source",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("workspace_guid", sa.String(36), nullable=False),
        sa.Column("workspace_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("organization", sa.String(200), nullable=False),
        sa.Column("project", sa.String(200), nullable=False, server_default=""),
        sa.Column("repository", sa.String(200), nullable=False),
        sa.Column("branch", sa.String(250), nullable=False, server_default="main"),
        sa.Column("directory", sa.String(500), nullable=False, server_default=""),
        sa.Column("secret_ref", sa.String(300), nullable=False, server_default=""),
        sa.Column("last_commit", sa.String(80), nullable=False, server_default=""),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "workspace_guid", name="uq_git_source_workspace"),
    )
    op.create_index("ix_git_source_customer_id", "git_source", ["customer_id"])
    op.create_index("ix_git_source_tenant_id", "git_source", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_git_source_tenant_id", "git_source")
    op.drop_index("ix_git_source_customer_id", "git_source")
    op.drop_table("git_source")
