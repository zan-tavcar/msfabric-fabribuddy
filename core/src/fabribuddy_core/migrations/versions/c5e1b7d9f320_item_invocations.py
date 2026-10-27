"""Item invocations (pipeline -> notebook/dataflow/pipeline) for orchestration

Revision ID: c5e1b7d9f320
Revises: a8d2f6c3e915
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "c5e1b7d9f320"
down_revision = "a8d2f6c3e915"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_invocation",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column(
            "from_item_id",
            sa.String(120),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_item_id",
            sa.String(120),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("from_item_id", "to_item_id", name="uq_item_invocation"),
    )
    for column in ("customer_id", "tenant_id", "from_item_id", "to_item_id"):
        op.create_index(f"ix_item_invocation_{column}", "item_invocation", [column])


def downgrade() -> None:
    for column in ("customer_id", "tenant_id", "from_item_id", "to_item_id"):
        op.drop_index(f"ix_item_invocation_{column}", "item_invocation")
    op.drop_table("item_invocation")
