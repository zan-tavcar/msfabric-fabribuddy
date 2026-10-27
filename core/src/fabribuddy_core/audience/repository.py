"""Audience scan runs: start, finish, find. Every read is scoped by customer."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ConnectedTenant, utcnow
from .models import KIND_SCAN, STATUS_FAILED, STATUS_RUNNING, ScanRun


def start_run(
    session: Session, tenant: ConnectedTenant, kind: str, params: dict | None = None
) -> ScanRun:
    run = ScanRun(
        tenant_id=tenant.id,
        customer_id=tenant.customer_id,
        kind=kind,
        status=STATUS_RUNNING,
        params=params or {},
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def finish_run(
    session: Session,
    run: ScanRun,
    *,
    status: str,
    payload: dict | None = None,
    error: str | None = None,
) -> ScanRun:
    run.status = status
    run.payload = payload
    run.error = error
    run.finished_at = utcnow()
    session.commit()
    session.refresh(run)
    return run


def latest_run(
    session: Session,
    customer_id: str,
    tenant_id: str,
    kind: str = KIND_SCAN,
    *,
    only_ok: bool = True,
) -> ScanRun | None:
    from .models import STATUS_OK

    stmt = select(ScanRun).where(
        ScanRun.customer_id == customer_id,
        ScanRun.tenant_id == tenant_id,
        ScanRun.kind == kind,
    )
    if only_ok:
        stmt = stmt.where(ScanRun.status == STATUS_OK)
    return session.scalars(stmt.order_by(ScanRun.started_at.desc())).first()


def running_run(session: Session, customer_id: str, tenant_id: str, kind: str):
    """An in-flight run of this kind, if any."""
    stmt = (
        select(ScanRun)
        .where(
            ScanRun.customer_id == customer_id,
            ScanRun.tenant_id == tenant_id,
            ScanRun.kind == kind,
            ScanRun.status == STATUS_RUNNING,
        )
        .order_by(ScanRun.started_at.desc())
    )
    return session.scalars(stmt).first()


def reap_orphaned_runs(session: Session) -> int:
    """Mark runs left 'running' by a killed process as failed."""
    n = 0
    for run in session.scalars(select(ScanRun).where(ScanRun.status == STATUS_RUNNING)):
        run.status = STATUS_FAILED
        run.error = "Interrupted — the server stopped before this scan finished."
        run.finished_at = utcnow()
        n += 1
    if n:
        session.commit()
    return n


def list_runs(
    session: Session, customer_id: str, tenant_id: str, limit: int = 20
) -> list[ScanRun]:
    stmt = (
        select(ScanRun)
        .where(ScanRun.customer_id == customer_id, ScanRun.tenant_id == tenant_id)
        .order_by(ScanRun.started_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))
