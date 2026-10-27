"""Organisation sign-up source (first-touch attribution)

Revision ID: a2b7e3d9f041
Revises: f1a6d2c8e930
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "a2b7e3d9f041"
down_revision = "f1a6d2c8e930"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customer") as batch:
        batch.add_column(sa.Column("signup_source", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customer") as batch:
        batch.drop_column("signup_source")
