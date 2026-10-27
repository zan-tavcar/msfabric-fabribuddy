"""Customer (organisation) rows with plan and subscription state

Revision ID: e2c4a7f1b893
Revises: b7e3d9a1c254
Create Date: 2026-09-28
"""

import datetime as dt

import sqlalchemy as sa
from alembic import op

revision = "e2c4a7f1b893"
down_revision = "b7e3d9a1c254"
branch_labels = None
depends_on = None

TRIAL_DAYS = 14


def upgrade() -> None:
    bind = op.get_bind()
    # A local SQLite database may already have the table from create_all (a
    # dev server started on the new models); only the backfill is then owed.
    if not sa.inspect(bind).has_table("customer"):
        _create()
    _backfill(bind)


def _create() -> None:
    op.create_table(
        "customer",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("plan_code", sa.String(30), nullable=True, server_default="trial"),
        sa.Column(
            "subscription_status", sa.String(20), nullable=True, server_default="trialing"
        ),
        sa.Column("trial_ends_at", sa.DateTime(), nullable=True),
        sa.Column("billing_period", sa.String(10), nullable=True, server_default="monthly"),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("billing_ref", sa.String(120), nullable=True, server_default=""),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_platform_admin", sa.Integer(), nullable=True, server_default="0"),
    )


def _backfill(bind) -> None:
    """
    Every organisation that already exists starts a fresh trial today, named
    after its first account's e-mail domain. Nobody loses access on upgrade.
    """
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    ids = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT customer_id FROM user_account "
                "UNION SELECT customer_id FROM connected_tenant"
            )
        )
    }
    table = sa.table(
        "customer",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("plan_code", sa.String),
        sa.column("subscription_status", sa.String),
        sa.column("trial_ends_at", sa.DateTime),
        sa.column("billing_period", sa.String),
        sa.column("billing_ref", sa.String),
        sa.column("notes", sa.Text),
        sa.column("is_platform_admin", sa.Integer),
    )
    existing = {row[0] for row in bind.execute(sa.text("SELECT id FROM customer"))}
    rows = []
    for customer_id in sorted(i for i in ids if i and i not in existing):
        email = bind.execute(
            sa.text(
                "SELECT email FROM user_account WHERE customer_id = :c "
                "ORDER BY created_at LIMIT 1"
            ),
            {"c": customer_id},
        ).scalar()
        rows.append(
            {
                "id": customer_id,
                "name": email.split("@")[-1] if email else customer_id,
                "created_at": now,
                "plan_code": "trial",
                "subscription_status": "trialing",
                "trial_ends_at": now + dt.timedelta(days=TRIAL_DAYS),
                "billing_period": "monthly",
                "billing_ref": "",
                "notes": "",
                "is_platform_admin": 0,
            }
        )
    if rows:
        op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_table("customer")
