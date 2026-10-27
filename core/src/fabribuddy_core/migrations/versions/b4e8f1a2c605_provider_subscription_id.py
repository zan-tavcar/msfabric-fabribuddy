"""Payment provider's subscription id on the subscription row (Paddle)

Revision ID: b4e8f1a2c605
Revises: a7d3e9c2f114
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op

revision = "b4e8f1a2c605"
down_revision = "a7d3e9c2f114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.add_column(
            sa.Column(
                "provider_subscription_id", sa.String(80), nullable=False, server_default=""
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.drop_column("provider_subscription_id")
