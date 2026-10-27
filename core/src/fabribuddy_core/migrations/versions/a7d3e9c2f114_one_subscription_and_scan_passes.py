"""One subscription per organisation (workspace-tiered plans); scan passes

Revision ID: a7d3e9c2f114
Revises: e6a1c4d8b203
Create Date: 2026-09-29

Each organisation had a subscription row per product (lineage, audience). The
products are now sold together, so the rows collapse into one (`fabribuddy`),
keeping the strongest: a paid plan beats a trial, a live one an ended one.
Every old paid code had no workspace limit, so it becomes `enterprise` — nobody
loses scope in the move.
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d3e9c2f114"
down_revision = "e6a1c4d8b203"
branch_labels = None
depends_on = None

_STATUS_RANK = {"active": 4, "past_due": 3, "trialing": 2, "canceled": 1, "expired": 0}


def upgrade() -> None:
    op.create_table(
        "scan_pass",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("connected_tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("incremental", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("products", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("steps_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_scan_pass_customer_id", "scan_pass", ["customer_id"])
    op.create_index("ix_scan_pass_started", "scan_pass", ["customer_id", "started_at"])

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, customer_id, product, plan_code, subscription_status, "
            "trial_ends_at, current_period_end FROM subscription"
        )
    ).fetchall()
    by_customer: dict[str, list] = {}
    for row in rows:
        by_customer.setdefault(row.customer_id, []).append(row)
    for customer_id, subs in by_customer.items():
        keep = max(
            subs,
            key=lambda r: (
                _STATUS_RANK.get(r.subscription_status, 0),
                r.plan_code not in ("trial", "", None),
                str(r.current_period_end or r.trial_ends_at or ""),
            ),
        )
        plan = keep.plan_code if keep.plan_code in ("trial", "team", "business") else (
            "trial" if keep.plan_code in ("", None) else "enterprise"
        )
        for row in subs:
            if row.id != keep.id:
                bind.execute(sa.text("DELETE FROM subscription WHERE id = :id"), {"id": row.id})
        bind.execute(
            sa.text("UPDATE subscription SET product = 'fabribuddy', plan_code = :plan WHERE id = :id"),
            {"plan": plan, "id": keep.id},
        )


def downgrade() -> None:
    op.drop_index("ix_scan_pass_started", "scan_pass")
    op.drop_index("ix_scan_pass_customer_id", "scan_pass")
    op.drop_table("scan_pass")
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT customer_id, plan_code, subscription_status, trial_ends_at, "
            "current_period_end, billing_period, created_at FROM subscription "
            "WHERE product = 'fabribuddy'"
        )
    ).fetchall()
    bind.execute(sa.text("DELETE FROM subscription WHERE product = 'fabribuddy'"))
    for row in rows:
        plan = "trial" if row.plan_code == "trial" else "tenant"
        for product in ("lineage", "audience"):
            bind.execute(
                sa.text(
                    "INSERT INTO subscription (customer_id, product, plan_code, "
                    "subscription_status, trial_ends_at, billing_period, "
                    "current_period_end, created_at) VALUES (:c, :p, :plan, :s, :t, "
                    ":b, :e, :created)"
                ),
                {
                    "c": row.customer_id,
                    "p": product,
                    "plan": plan,
                    "s": row.subscription_status,
                    "t": row.trial_ends_at,
                    "b": row.billing_period,
                    "e": row.current_period_end,
                    "created": row.created_at,
                },
            )
