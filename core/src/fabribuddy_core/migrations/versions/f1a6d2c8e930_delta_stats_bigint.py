"""Delta stats and versions as 64-bit integers

Revision ID: f1a6d2c8e930
Revises: e8f4c0b5d629
Create Date: 2026-10-06

Rows, bytes and file counts of a real table pass 2,147,483,647 (2.1 GB is
enough for size_bytes); Postgres refused the insert ("integer out of range")
and the Delta step failed for the whole tenant.
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a6d2c8e930"
down_revision = "e8f4c0b5d629"
branch_labels = None
depends_on = None

_STATS = ("current_version", "num_rows", "size_bytes", "num_files")


def upgrade() -> None:
    with op.batch_alter_table("delta_stats") as batch:
        for column in _STATS:
            batch.alter_column(column, type_=sa.BigInteger(), existing_type=sa.Integer())
    with op.batch_alter_table("delta_version") as batch:
        batch.alter_column(
            "version", type_=sa.BigInteger(), existing_type=sa.Integer(), existing_nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("delta_version") as batch:
        batch.alter_column(
            "version", type_=sa.Integer(), existing_type=sa.BigInteger(), existing_nullable=False
        )
    with op.batch_alter_table("delta_stats") as batch:
        for column in _STATS:
            batch.alter_column(column, type_=sa.Integer(), existing_type=sa.BigInteger())
