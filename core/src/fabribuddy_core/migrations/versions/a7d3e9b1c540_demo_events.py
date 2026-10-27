"""Demo visits and the pages demo visitors open

Revision ID: a7d3e9b1c540
Revises: d5e1b6a3c274
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d3e9b1c540"
down_revision = "d5e1b6a3c274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(), nullable=True),
        sa.Column("visit", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("label", sa.String(80), nullable=True),
    )
    op.create_index("ix_demo_event_at", "demo_event", ["at"])
    op.create_index("ix_demo_event_visit", "demo_event", ["visit"])


def downgrade() -> None:
    op.drop_index("ix_demo_event_visit", table_name="demo_event")
    op.drop_index("ix_demo_event_at", table_name="demo_event")
    op.drop_table("demo_event")
