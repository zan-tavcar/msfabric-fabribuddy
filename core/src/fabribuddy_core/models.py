"""
Core (non-lineage) persistence: a connected Fabric tenant and the collector
runs made against it.

The lineage graph itself lives in `app/lineage/models.py`. Splitting them keeps
this file about *the connection*, which is the part that holds credentials and
therefore the part worth reading closely.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> dt.datetime:
    """
    Naive UTC.

    Every DateTime column here is naive, and SQLite hands values back naive, so
    an aware "now" cannot be compared with anything read from the database
    without a TypeError. Storing naive UTC everywhere makes the comparison work
    and keeps one rule: values in this database are UTC, without a tzinfo.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# Tenant connection states
STATUS_UNVALIDATED = "unvalidated"
STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"

# Collector run kinds. One per independent, idempotent collector.
KIND_SCANNER = "scanner"  # Power BI Scanner API (getInfo)
KIND_ITEMS = "items"  # Fabric REST item inventory
KIND_DEFINITIONS = "definitions"  # getDefinition -> code_artifact
KIND_DELTA = "delta"  # OneLake / Delta log
KIND_JOBS = "jobs"  # job run history
KIND_ACCESS = "access"  # access resolution
KIND_PARSE = "parse"  # re-run parsers over stored artifacts
KIND_WAREHOUSE_SQL = "warehouse_sql"  # T-SQL modules + query insights, read-only

ALL_KINDS = (
    KIND_SCANNER,
    KIND_ITEMS,
    KIND_DEFINITIONS,
    KIND_DELTA,
    KIND_JOBS,
    KIND_ACCESS,
    KIND_PARSE,
    KIND_WAREHOUSE_SQL,
)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"
# Collectors report this when they finished but some items failed — the
# difference between "we have everything" and "we have most of it" is the
# whole point of per-item error capture.
STATUS_PARTIAL = "partial"


class ConnectedTenant(Base):
    """
    A customer's Fabric tenant plus a *reference* to its SPN secret.

    The ClientSecret itself is never stored here — `secret_ref` points at the
    secret store ('env:VAR_NAME' or 'local:<uuid>' today, a Key Vault URI
    later). TenantId and ClientId are not secrets and are stored directly.
    """

    __tablename__ = "connected_tenant"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    azure_tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(300), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_UNVALIDATED)
    # {"powerbi_admin": {...}, "fabric_api": {...}, "onelake": {...}} — which
    # credential tier actually validated, so the UI can say what is missing.
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)

    # The effective scan scope, recomputed each scan: workspaces where the SPN
    # is at least Contributor, narrowed by the Admin page's selection.
    contributor_workspaces: Mapped[list] = mapped_column(JSON, default=list)

    # Admin page: scan every workspace the SPN can reach ("all"), or only the
    # ones listed in selected_workspaces ("selected").
    scope_mode: Mapped[str] = mapped_column(String(20), default="all")
    selected_workspaces: Mapped[list] = mapped_column(JSON, default=list)
    # Audience: include personal ("My workspace") workspaces. Noise for most audits.
    include_personal: Mapped[bool] = mapped_column(Integer, default=0)

    # {"enabled": bool, "times": ["06:00", …] (≤ 8), "timezone": "Europe/…",
    #  "fullWeekday": 6}  — scheduled scans are incremental, except a weekly full one.
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    last_scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    last_validated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_demo: Mapped[bool] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    runs: Mapped[list["CollectorRun"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("customer_id", "azure_tenant_id", name="uq_customer_tenant"),
    )


class CollectorRun(Base):
    """
    One execution of one collector.

    The payload holds *stats only*, never the collected data — a tenant has
    thousands of tables and tens of thousands of edges, which belong in the
    normalised tables where they can be queried, not in a JSON blob.
    """

    __tablename__ = "collector_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connected_tenant.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    params: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)

    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    tenant: Mapped[ConnectedTenant] = relationship(back_populates="runs")
    errors: Mapped[list["CollectorError"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_collector_run_lookup", "tenant_id", "kind", "status", "started_at"),
    )


class CollectorError(Base):
    """
    One item that failed inside an otherwise successful collector run.

    Partial failure must never break a scan: a notebook with a protected
    sensitivity label, or a workspace the SPN is not Contributor on, costs us
    that one item and is surfaced in the UI — not the other nine hundred.
    """

    __tablename__ = "collector_error"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collector_run.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    collector: Mapped[str] = mapped_column(String(20), nullable=False)
    # What we were working on: a workspace, an item, or a table node.
    scope: Mapped[str] = mapped_column(String(40), default="")
    scope_id: Mapped[str] = mapped_column(String(400), default="")
    scope_name: Mapped[str] = mapped_column(String(400), default="")

    # Machine-readable so the UI can offer the right fix ('needs_contributor',
    # 'protected_label', 'rate_limited', 'unsupported', 'error').
    reason: Mapped[str] = mapped_column(String(40), default="error")
    message: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[CollectorRun] = relationship(back_populates="errors")

    __table_args__ = (Index("ix_collector_error_tenant", "tenant_id", "collector"),)


# --- accounts ----------------------------------------------------------------
# How a scan pass was started.
TRIGGER_MANUAL = "manual"  # Rescan, Settings -> Scan now
TRIGGER_SCHEDULE = "schedule"
TRIGGER_API = "api"  # POST /api/admin/scan?wait=true, scripts


class ScanPass(Base):
    """
    One scan pass: every product the organisation has, one after the other.

    The unit a person thinks in ("the 06:00 scan took 9 minutes and failed"),
    where collector_run and audience_scan_run are the unit the code thinks in
    (one step each). The console's scan history reads these rows; the live
    progress of a running pass is kept in memory (services/progress.py).
    """

    __tablename__ = "scan_pass"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connected_tenant.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default=TRIGGER_MANUAL)
    incremental: Mapped[bool] = mapped_column(Integer, default=1)
    products: Mapped[list] = mapped_column(JSON, default=list)
    # running | ok | partial | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    steps_done: Mapped[int] = mapped_column(Integer, default=0)
    steps_failed: Mapped[int] = mapped_column(Integer, default=0)
    # Per product and step: status and seconds taken. Stats only, never data.
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_scan_pass_started", "customer_id", "started_at"),)


class UserAccount(Base):
    """
    A person who signs in. `customer_id` is the organisation whose data they see.

    One organisation per registration today; the column is what an invitation
    flow would share between accounts later.
    """

    __tablename__ = "user_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # scrypt, see app/auth.py. Never a reversible form. Empty for an account
    # that signs in only with Microsoft (it never verifies).
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    # The Microsoft (Entra) identity this account signs in with: object id and
    # tenant id from the id token. The pair, never the e-mail claim, identifies
    # the person — a tenant admin can set any e-mail on a user.
    ms_oid: Mapped[str] = mapped_column(String(64), default="", index=True)
    ms_tid: Mapped[str] = mapped_column(String(64), default="")
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # admin: connection, scope, schedule, members. user: read-only.
    role: Mapped[str] = mapped_column(String(20), default="admin")
    email_verified: Mapped[bool] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Last authenticated request, refreshed at most every few minutes. What the
    # operator's console counts as "active"; sign-in alone misses long sessions.
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class UserSession(Base):
    """A signed-in browser. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "user_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class AuthToken(Base):
    """One-time e-mail tokens (verify, reset). Hashed, single use, short-lived."""

    __tablename__ = "auth_token"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_account.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)  # verify | reset
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# Admin: settings — the connection, which workspaces are scanned, the schedule,
#        scans, members, the plan. Contributor: everything a viewer does, plus
#        writing table and column descriptions. Viewer: read-only.
ROLE_ADMIN = "admin"
ROLE_CONTRIBUTOR = "contributor"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_CONTRIBUTOR, ROLE_VIEWER)
# Before contributors existed the read-only role was called "user".
LEGACY_ROLES = {"user": ROLE_VIEWER}
MAX_ADMINS = 5


class Invitation(Base):
    """
    An admin's invitation for someone to join their organisation.

    Only the SHA-256 of the link token is stored; the link itself is shown to
    the inviting admin once and e-mailed when SMTP is configured. Accepting it
    creates the account in the inviter's organisation with the invited role —
    and proves the address, since the link only reaches that inbox.
    """

    __tablename__ = "invitation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_VIEWER)
    invited_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("user_account.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class SchedulerLease(Base):
    """
    Which process runs the scheduler. With several app instances only the lease
    holder fires scans, so a schedule never runs twice.
    """

    __tablename__ = "scheduler_lease"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    holder: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


# --- the paying organisation --------------------------------------------------
# Subscription states, mirrored from the payment provider once one is wired.
SUB_TRIALING = "trialing"
SUB_ACTIVE = "active"
SUB_PAST_DUE = "past_due"
SUB_CANCELED = "canceled"
SUB_STATUSES = (SUB_TRIALING, SUB_ACTIVE, SUB_PAST_DUE, SUB_CANCELED)


class Customer(Base):
    """
    An organisation — the row behind every `customer_id` in this database.

    What it pays for is in `subscription`, one row per product. Its id is the
    same string every other table carries, so accounts, tenants and both
    products' data hang off it without a join table.
    """

    __tablename__ = "customer"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    # The payment provider's customer id (Stripe `cus_…`): one per organisation,
    # shared by both products' subscriptions.
    billing_ref: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    # Operator flag: this organisation's members see the control plane. Never
    # settable through a customer-facing API — only FL_PLATFORM_ADMIN_EMAIL or
    # a direct database update.
    is_platform_admin: Mapped[bool] = mapped_column(Integer, default=0)
    # Where the organisation came from, first touch: ref, utm_*, gclid, the
    # site's referrer and landing page. Marketing attribution, never content.
    signup_source: Mapped[dict] = mapped_column(JSON, default=dict)


class Subscription(Base):
    """
    One organisation's subscription, covering both products.

    Plan contents live in `app/plans.py`; this row stores which plan and where
    the subscription stands. Created on registration as a trial. `product` is
    always `fabribuddy` now (plans.SUITE); it once held lineage | audience, when
    each product was sold separately.
    """

    __tablename__ = "subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("customer.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product: Mapped[str] = mapped_column(String(20), nullable=False)  # fabribuddy
    plan_code: Mapped[str] = mapped_column(String(30), default="trial")
    subscription_status: Mapped[str] = mapped_column(String(20), default=SUB_TRIALING)
    trial_ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    billing_period: Mapped[str] = mapped_column(String(10), default="monthly")
    # End of the paid period — the paying equivalent of trial_ends_at.
    current_period_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # The payment provider's id for this subscription (Paddle `sub_…`), so a
    # plan change can be made on it rather than by selling a second one.
    provider_subscription_id: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("customer_id", "product", name="uq_subscription"),)


class DemoEvent(Base):
    """
    One visit to app.fabribuddy.com/demo, or one page a demo visitor opened.

    `visit` is a hash of the demo session, never the session itself, so it
    joins a visit to its pages and to nothing else. No address, no person.
    """

    __tablename__ = "demo_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    visit: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # visit (a new demo session) | return (a browser that already had one) | view
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # For a visit: where it came from (attribution.label); for a view: the page.
    label: Mapped[str] = mapped_column(String(80), default="")
