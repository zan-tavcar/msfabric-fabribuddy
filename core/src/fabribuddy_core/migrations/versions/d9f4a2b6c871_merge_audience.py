"""Merge FabriBuddy Audience: per-product subscriptions and Audience's tables

Revision ID: d9f4a2b6c871
Revises: c5e1b7d9f320
Create Date: 2026-09-28

Lineage and Audience become one app. What an organisation pays for moves from
columns on `customer` to one `subscription` row per product: the existing plan
becomes the Lineage subscription, and every organisation gets a fresh 14-day
Audience trial. Audience's scan runs and audit-log events get their tables; the
connected tenant gains Audience's "include personal workspaces" switch.
"""

import datetime as dt

import sqlalchemy as sa
from alembic import op

revision = "d9f4a2b6c871"
down_revision = "c5e1b7d9f320"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            sa.String(64),
            sa.ForeignKey("customer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product", sa.String(20), nullable=False),
        sa.Column("plan_code", sa.String(30), nullable=False, server_default="trial"),
        sa.Column("subscription_status", sa.String(20), nullable=False, server_default="trialing"),
        sa.Column("trial_ends_at", sa.DateTime(), nullable=True),
        sa.Column("billing_period", sa.String(10), nullable=False, server_default="monthly"),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("customer_id", "product", name="uq_subscription"),
    )
    op.create_index("ix_subscription_customer_id", "subscription", ["customer_id"])

    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    bind = op.get_bind()
    customers = bind.execute(
        sa.text(
            "SELECT id, plan_code, subscription_status, trial_ends_at, billing_period, "
            "current_period_end FROM customer"
        )
    ).fetchall()
    insert = sa.text(
        "INSERT INTO subscription (customer_id, product, plan_code, subscription_status, "
        "trial_ends_at, billing_period, current_period_end, created_at) VALUES "
        "(:c, :p, :plan, :status, :trial, 'monthly', :period, :now)"
    )
    for row in customers:
        plan = row.plan_code if row.plan_code in ("trial", "tenant") else "tenant"
        bind.execute(
            insert,
            {
                "c": row.id,
                "p": "lineage",
                "plan": plan,
                "status": row.subscription_status or "trialing",
                "trial": row.trial_ends_at,
                "period": row.current_period_end,
                "now": now,
            },
        )
        bind.execute(
            insert,
            {
                "c": row.id,
                "p": "audience",
                "plan": "trial",
                "status": "trialing",
                "trial": now + dt.timedelta(days=14),
                "period": None,
                "now": now,
            },
        )

    with op.batch_alter_table("customer") as batch:
        for column in (
            "plan_code",
            "subscription_status",
            "trial_ends_at",
            "billing_period",
            "current_period_end",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("connected_tenant") as batch:
        batch.add_column(
            sa.Column("include_personal", sa.Integer(), nullable=False, server_default="0")
        )

    op.create_table(
        "audience_scan_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("connected_tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_audience_scan_run_customer_id", "audience_scan_run", ["customer_id"])
    op.create_index(
        "ix_audience_scan_run_lookup",
        "audience_scan_run",
        ["tenant_id", "kind", "status", "started_at"],
    )

    op.create_table(
        "activity_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("connected_tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("event_key", sa.String(80), nullable=False),
        sa.Column("user_upn", sa.String(320), nullable=False, server_default=""),
        sa.Column("action", sa.String(80), nullable=False, server_default=""),
        sa.Column("item_name", sa.String(400), nullable=False, server_default=""),
        sa.Column("workspace_name", sa.String(400), nullable=False, server_default=""),
        sa.UniqueConstraint("tenant_id", "event_key", name="uq_activity_event"),
    )
    op.create_index("ix_activity_event_customer_id", "activity_event", ["customer_id"])
    op.create_index("ix_activity_tenant_ts", "activity_event", ["tenant_id", "ts"])
    op.create_index(
        "ix_activity_tenant_action_ts", "activity_event", ["tenant_id", "action", "ts"]
    )


def downgrade() -> None:
    op.drop_table("activity_event")
    op.drop_table("audience_scan_run")
    with op.batch_alter_table("connected_tenant") as batch:
        batch.drop_column("include_personal")
    with op.batch_alter_table("customer") as batch:
        batch.add_column(sa.Column("plan_code", sa.String(30), server_default="trial"))
        batch.add_column(sa.Column("subscription_status", sa.String(20), server_default="trialing"))
        batch.add_column(sa.Column("trial_ends_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("billing_period", sa.String(10), server_default="monthly"))
        batch.add_column(sa.Column("current_period_end", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE customer SET (plan_code, subscription_status, trial_ends_at, "
        "current_period_end) = (SELECT plan_code, subscription_status, trial_ends_at, "
        "current_period_end FROM subscription s WHERE s.customer_id = customer.id "
        "AND s.product = 'lineage')"
    )
    op.drop_table("subscription")
