"""Tenants connect with a service principal only

Revision ID: d5e1b6a3c274
Revises: c4d9a5f2b163
Create Date: 2026-10-08

Connecting a tenant by signing in with Microsoft (b3c8f4e1a052) is withdrawn.
A tenant connected that way keeps its collected data, but its stored sign-in
is removed and it shows as needing a service principal (Settings → Connection).
"""

import sqlalchemy as sa
from alembic import op

revision = "d5e1b6a3c274"
down_revision = "c4d9a5f2b163"
branch_labels = None
depends_on = None

_RECONNECT = (
    "This tenant was connected by signing in with Microsoft, which FabriBuddy no "
    "longer offers. Enter a service principal under Settings → Connection."
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, secret_ref FROM connected_tenant WHERE auth_mode = 'user'")
    ).fetchall()
    for tenant_id, secret_ref in rows:
        store, _, key = (secret_ref or "").partition(":")
        if store == "encrypted" and key:
            bind.execute(sa.text("DELETE FROM secret_blob WHERE key = :k"), {"k": key})
        bind.execute(
            sa.text(
                "UPDATE connected_tenant SET secret_ref = '', client_id = '', "
                "status = 'error', last_validation_error = :msg WHERE id = :id"
            ),
            {"msg": _RECONNECT, "id": tenant_id},
        )
    with op.batch_alter_table("connected_tenant") as batch:
        batch.drop_column("signed_in_as")
        batch.drop_column("auth_mode")


def downgrade() -> None:
    with op.batch_alter_table("connected_tenant") as batch:
        batch.add_column(
            sa.Column("auth_mode", sa.String(10), nullable=False, server_default="spn")
        )
        batch.add_column(
            sa.Column("signed_in_as", sa.String(200), nullable=False, server_default="")
        )
