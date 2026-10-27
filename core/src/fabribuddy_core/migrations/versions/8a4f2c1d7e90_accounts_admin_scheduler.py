"""Accounts, Admin settings, scheduler lease, encrypted secrets

Revision ID: 8a4f2c1d7e90
Revises: 5d1e7a2c9b40
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "8a4f2c1d7e90"
down_revision = "5d1e7a2c9b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connected_tenant") as batch:
        batch.add_column(sa.Column("scope_mode", sa.String(20), nullable=False, server_default="all"))
        batch.add_column(sa.Column("selected_workspaces", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("schedule", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("last_scheduled_at", sa.DateTime(), nullable=True))

    op.create_table(
        "user_account",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(300), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="owner"),
        sa.Column("email_verified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_user_account_customer_id", "user_account", ["customer_id"])

    op.create_table(
        "user_session",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])

    op.create_table(
        "auth_token",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "scheduler_lease",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("holder", sa.String(80), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "secret_blob",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("secret_blob")
    op.drop_table("scheduler_lease")
    op.drop_table("auth_token")
    op.drop_index("ix_user_session_user_id", "user_session")
    op.drop_table("user_session")
    op.drop_index("ix_user_account_customer_id", "user_account")
    op.drop_table("user_account")
    with op.batch_alter_table("connected_tenant") as batch:
        batch.drop_column("last_scheduled_at")
        batch.drop_column("schedule")
        batch.drop_column("selected_workspaces")
        batch.drop_column("scope_mode")
