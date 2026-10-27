"""Lanes by storage kind instead of medallion layer

Revision ID: 5d1e7a2c9b40
Revises: c6b31db782ad
Create Date: 2026-09-28

Data-only. A node's lane is now the storage it lives in, decided by its kind,
so every existing row is recomputed from `kind` and marked declared.
"""

from alembic import op

revision = "5d1e7a2c9b40"
down_revision = "c6b31db782ad"
branch_labels = None
depends_on = None

_LANE_BY_KIND = {
    "external": "source",
    "mirrored_table": "mirrored",
    "lakehouse_table": "lakehouse",
    "warehouse_table": "warehouse",
    "model_table": "semantic",
    "report_visual": "report",
}


def upgrade() -> None:
    cases = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in _LANE_BY_KIND.items())
    op.execute(
        f"UPDATE data_node SET layer = CASE kind {cases} ELSE 'lakehouse' END, "
        f"layer_is_declared = TRUE"  # TRUE, not 1: Postgres booleans reject an int
    )


def downgrade() -> None:
    # The medallion lanes were guesses from names and graph depth; they cannot
    # be reconstructed here. The next scan under the old code would recompute
    # them, so leave the storage lanes in place.
    pass
