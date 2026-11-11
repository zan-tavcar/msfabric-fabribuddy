"""Job Monitor: child activity runs and item schedules

Revision ID: b9c4e2a7d813
Revises: a7d3e9b1c540
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "b9c4e2a7d813"
down_revision = "a7d3e9b1c540"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_activity_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column(
            "job_run_id",
            sa.Integer(),
            sa.ForeignKey("job_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("activity_run_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("activity_type", sa.String(60), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("target_item_id", sa.String(120), nullable=True),
        sa.Column("target_run_guid", sa.String(80), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    for column in ("customer_id", "tenant_id", "job_run_id"):
        op.create_index(f"ix_job_activity_run_{column}", "job_activity_run", [column])

    op.create_table(
        "item_schedule",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column(
            "item_id",
            sa.String(120),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schedule_guid", sa.String(80), nullable=False, server_default=""),
        sa.Column("job_type", sa.String(40), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=True),
    )
    for column in ("customer_id", "tenant_id", "item_id"):
        op.create_index(f"ix_item_schedule_{column}", "item_schedule", [column])


def downgrade() -> None:
    for column in ("customer_id", "tenant_id", "item_id"):
        op.drop_index(f"ix_item_schedule_{column}", "item_schedule")
    op.drop_table("item_schedule")
    for column in ("customer_id", "tenant_id", "job_run_id"):
        op.drop_index(f"ix_job_activity_run_{column}", "job_activity_run")
    op.drop_table("job_activity_run")
