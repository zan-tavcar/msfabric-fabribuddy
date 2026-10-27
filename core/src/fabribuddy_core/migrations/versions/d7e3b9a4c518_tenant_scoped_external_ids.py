"""External source ids scoped to the tenant

Revision ID: d7e3b9a4c518
Revises: c5f2a9d1e730
Create Date: 2026-10-05

An external source's id came from its URL / server alone, so two tenants
reading the same API shared one row: the second tenant's scan updated the
first tenant's node and drew an empty box. Ids now include the tenant. The
old rows are removed here, with everything that points at them; the next scan
recreates every external source under its new id (parse and the shortcut step
draw them from the code and definitions they already hold).
"""

import sqlalchemy as sa
from alembic import op

revision = "d7e3b9a4c518"
down_revision = "c5f2a9d1e730"
branch_labels = None
depends_on = None

_EXTERNAL = "SELECT id FROM data_node WHERE kind = 'external'"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Every column that references data_node.id, children first, whatever
    # the foreign key's ON DELETE says.
    for table in inspector.get_table_names():
        if table == "data_node":
            continue
        for fk in inspector.get_foreign_keys(table):
            if fk.get("referred_table") != "data_node":
                continue
            for column in fk.get("constrained_columns") or []:
                op.execute(f"DELETE FROM {table} WHERE {column} IN ({_EXTERNAL})")
    op.execute("DELETE FROM data_node WHERE kind = 'external'")


def downgrade() -> None:
    # Nothing to restore: the next scan recreates external sources either way.
    pass
