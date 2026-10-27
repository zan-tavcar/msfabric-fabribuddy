"""One plan (FabriBuddy Lineage, tenant-wide, monthly); user last_seen_at

Revision ID: f3a9c1e7d402
Revises: e2c4a7f1b893
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a9c1e7d402"
down_revision = "e2c4a7f1b893"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every former tier becomes the one paid plan; billing is monthly.
    op.execute(
        "UPDATE customer SET plan_code = 'tenant' "
        "WHERE plan_code IN ('starter', 'growth', 'enterprise')"
    )
    op.execute("UPDATE customer SET billing_period = 'monthly'")
    with op.batch_alter_table("user_account") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_account") as batch:
        batch.drop_column("last_seen_at")
    op.execute("UPDATE customer SET plan_code = 'growth' WHERE plan_code = 'tenant'")
