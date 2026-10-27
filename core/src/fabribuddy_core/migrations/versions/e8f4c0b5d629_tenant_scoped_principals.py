"""Principal ids scoped to the tenant

Revision ID: e8f4c0b5d629
Revises: d7e3b9a4c518
Create Date: 2026-10-05

A principal's id was its Entra object id or, failing that, its e-mail; a guest
invited into two customers' tenants (or a multi-tenant app) produced one row
for both. Ids now include the tenant. Grants are rebuilt at every scan, so the
old rows and everything referencing them are simply removed.
"""

import sqlalchemy as sa
from alembic import op

revision = "e8f4c0b5d629"
down_revision = "d7e3b9a4c518"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in inspector.get_table_names():
        if table == "principal":
            continue
        for fk in inspector.get_foreign_keys(table):
            if fk.get("referred_table") == "principal":
                for column in fk.get("constrained_columns") or []:
                    op.execute(f"DELETE FROM {table} WHERE {column} IS NOT NULL")
    op.execute("DELETE FROM principal")


def downgrade() -> None:
    pass
