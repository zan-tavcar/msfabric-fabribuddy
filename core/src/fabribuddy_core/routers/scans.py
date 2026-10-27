"""
Scan progress and history for the signed-in organisation.

Any member may read it (a read-only user should know a scan is underway just as
much as the admin who started it). Starting a scan stays admin-only, in
routers/admin.py.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import default_tenant
from ..models import ConnectedTenant, ScanPass
from ..services import progress

router = APIRouter(prefix="/api/scan", tags=["scans"])


def pass_out(row: ScanPass) -> dict:
    """One pass as the history shows it: when, how long, how it ended."""
    seconds = (
        round((row.finished_at - row.started_at).total_seconds())
        if row.finished_at and row.started_at
        else None
    )
    return {
        "id": row.id,
        "trigger": row.trigger,
        "incremental": bool(row.incremental),
        "products": row.products or [],
        "status": row.status,
        "startedAt": row.started_at.isoformat() if row.started_at else None,
        "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
        "durationSeconds": seconds,
        "stepsTotal": row.steps_total,
        "stepsDone": row.steps_done,
        "stepsFailed": row.steps_failed,
        "outcome": row.outcome or {},
    }


@router.get("/progress")
def scan_progress(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """
    The running pass (what it is reading, and how far along), or null, plus
    the last finished one. Cheap: polled every few seconds while a scan runs.
    """
    live = progress.snapshot(tenant.id)
    if live is None:
        # A pass started by another process (a second worker, a restart
        # mid-scan) has no live progress here, but its row still says running.
        running = session.scalars(
            select(ScanPass)
            .where(ScanPass.tenant_id == tenant.id, ScanPass.status == "running")
            .order_by(ScanPass.started_at.desc())
        ).first()
        if running is not None:
            live = {
                "passId": running.id,
                "startedAt": running.started_at.isoformat(),
                "stepsTotal": running.steps_total,
                "stepsDone": running.steps_done,
                "label": "Scanning…",
                "percent": round(100 * running.steps_done / max(1, running.steps_total)),
            }
    last = session.scalars(
        select(ScanPass)
        .where(ScanPass.tenant_id == tenant.id, ScanPass.status != "running")
        .order_by(ScanPass.started_at.desc())
    ).first()
    return {
        "running": live is not None,
        "pass": live,
        "last": pass_out(last) if last else None,
    }


@router.get("/history")
def scan_history(
    limit: int = Query(20, ge=1, le=200),
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    rows = session.scalars(
        select(ScanPass)
        .where(ScanPass.tenant_id == tenant.id)
        .order_by(ScanPass.started_at.desc())
        .limit(limit)
    )
    return [pass_out(r) for r in rows]
