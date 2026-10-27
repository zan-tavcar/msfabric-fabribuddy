"""
Audience's own tables: the scan runs it makes and the audit-log events it keeps.

Scan results are stored the way the scanners return them — a run payload per
kind (inventory, permissions, licences) — and served as-is. Activity is rows,
because the audit log is only ever fetched incrementally. Both hang off the
platform's `connected_tenant`: one tenant, both products.
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
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import utcnow

# Scan kinds. Each is independent and runs at most once at a time per tenant.
KIND_SCAN = "scan"  # inventory: workspaces, reports, models, users, refresh state
KIND_PERMISSIONS = "permissions"  # per-item grants + Entra group expansion
KIND_ACTIVITY = "activity"  # audit log, incremental into activity_event
KIND_LICENSES = "licenses"  # Pro / PPU seats via Microsoft Graph
ALL_KINDS = (KIND_SCAN, KIND_PERMISSIONS, KIND_ACTIVITY, KIND_LICENSES)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"


def _uuid() -> str:
    return str(uuid.uuid4())


class ScanRun(Base):
    """One execution of an Audience scan kind, with its result payload."""

    __tablename__ = "audience_scan_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connected_tenant.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised so every read can filter by customer without a join.
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    params: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_audience_scan_run_lookup", "tenant_id", "kind", "status", "started_at"),
    )


class ActivityEvent(Base):
    """
    One audit-log event, stored as a row.

    As rows we can ask "what is the newest event we hold?" and fetch only from
    there, instead of re-reading 30 days (~9 minutes) to learn about one day.
    `event_key` is the audit log's own record id, so re-fetching an overlapping
    day is idempotent.
    """

    __tablename__ = "activity_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connected_tenant.id", ondelete="CASCADE"), nullable=False
    )

    ts: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    event_key: Mapped[str] = mapped_column(String(80), nullable=False)

    user_upn: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(80), default="")
    item_name: Mapped[str] = mapped_column(String(400), default="")
    workspace_name: Mapped[str] = mapped_column(String(400), default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", "event_key", name="uq_activity_event"),
        Index("ix_activity_tenant_ts", "tenant_id", "ts"),
        Index("ix_activity_tenant_action_ts", "tenant_id", "action", "ts"),
    )
