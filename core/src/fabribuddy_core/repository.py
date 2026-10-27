"""
Database access for connections and collector runs.

Every lookup takes a `customer_id` and filters on it. Today that is always the
sentinel from config, but keeping the parameter mandatory means isolation is
enforced here — at the data-access layer — rather than retrofitted route by
route once accounts exist. The lineage graph has its own repository module
under app/lineage/, following the same rule.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    CollectorError,
    CollectorRun,
    ConnectedTenant,
    utcnow,
)


# --- connected tenants -----------------------------------------------------
def list_tenants(session: Session, customer_id: str) -> list[ConnectedTenant]:
    stmt = (
        select(ConnectedTenant)
        .where(ConnectedTenant.customer_id == customer_id)
        .order_by(ConnectedTenant.created_at)
    )
    return list(session.scalars(stmt))


def get_tenant(
    session: Session, customer_id: str, tenant_id: str
) -> ConnectedTenant | None:
    stmt = select(ConnectedTenant).where(
        ConnectedTenant.customer_id == customer_id,
        ConnectedTenant.id == tenant_id,
    )
    return session.scalars(stmt).first()


def get_tenant_by_azure_id(
    session: Session, customer_id: str, azure_tenant_id: str
) -> ConnectedTenant | None:
    stmt = select(ConnectedTenant).where(
        ConnectedTenant.customer_id == customer_id,
        ConnectedTenant.azure_tenant_id == azure_tenant_id,
    )
    return session.scalars(stmt).first()


def upsert_tenant(
    session: Session,
    customer_id: str,
    *,
    display_name: str,
    azure_tenant_id: str,
    client_id: str,
    secret_ref: str,
    status: str,
    capabilities: dict,
    validation_error: str | None,
) -> ConnectedTenant:
    """Reconnecting the same Azure tenant updates the row rather than duplicating it."""
    tenant = get_tenant_by_azure_id(session, customer_id, azure_tenant_id)
    if tenant is None:
        tenant = ConnectedTenant(customer_id=customer_id, azure_tenant_id=azure_tenant_id)
        session.add(tenant)

    tenant.display_name = display_name
    tenant.client_id = client_id
    tenant.secret_ref = secret_ref
    tenant.status = status
    tenant.capabilities = capabilities
    tenant.last_validation_error = validation_error
    tenant.last_validated_at = utcnow()

    session.commit()
    session.refresh(tenant)
    return tenant


def delete_tenant(session: Session, tenant: ConnectedTenant) -> None:
    from .lineage import repository as lineage_repository

    # Disconnecting means forgetting: the collected estate goes with the
    # connection, not just the credentials.
    lineage_repository.delete_tenant_data(session, tenant.customer_id, tenant.id)
    session.delete(tenant)
    session.commit()


def set_contributor_workspaces(
    session: Session, tenant: ConnectedTenant, workspace_guids: list[str]
) -> ConnectedTenant:
    """
    Record which workspaces the customer opted into Contributor-tier collection for.

    Explicitly opt-in per workspace: code and Delta-log collection reach much
    further into a tenant than the base inventory, so it is never inferred.
    """
    tenant.contributor_workspaces = sorted(
        {g.strip().lower() for g in workspace_guids if g}
    )
    session.commit()
    session.refresh(tenant)
    return tenant


# --- collector runs --------------------------------------------------------
def start_run(
    session: Session, tenant: ConnectedTenant, kind: str, params: dict | None = None
) -> CollectorRun:
    run = CollectorRun(
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
    run: CollectorRun,
    *,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> CollectorRun:
    run.status = status
    run.stats = stats or {}
    run.error = error
    run.finished_at = utcnow()
    session.commit()
    session.refresh(run)
    return run


def record_errors(session: Session, run: CollectorRun, errors: list[dict]) -> int:
    """
    Persist per-item failures from a run.

    Written as rows, not folded into the run's error string, because the UI
    needs to attach each one to the node or item it concerns.
    """
    for entry in errors:
        session.add(
            CollectorError(
                run_id=run.id,
                customer_id=run.customer_id,
                tenant_id=run.tenant_id,
                collector=run.kind,
                scope=entry.get("scope", ""),
                scope_id=entry.get("scope_id", ""),
                scope_name=entry.get("scope_name", ""),
                reason=entry.get("reason", "error"),
                message=str(entry.get("message", ""))[:2000],
            )
        )
    if errors:
        session.commit()
    return len(errors)


def latest_run(
    session: Session,
    customer_id: str,
    tenant_id: str,
    kind: str,
    *,
    only_ok: bool = True,
) -> CollectorRun | None:
    stmt = select(CollectorRun).where(
        CollectorRun.customer_id == customer_id,
        CollectorRun.tenant_id == tenant_id,
        CollectorRun.kind == kind,
    )
    if only_ok:
        stmt = stmt.where(CollectorRun.status.in_((STATUS_OK, STATUS_PARTIAL)))
    return session.scalars(stmt.order_by(CollectorRun.started_at.desc())).first()


def running_run(
    session: Session, customer_id: str, tenant_id: str, kind: str
) -> CollectorRun | None:
    stmt = (
        select(CollectorRun)
        .where(
            CollectorRun.customer_id == customer_id,
            CollectorRun.tenant_id == tenant_id,
            CollectorRun.kind == kind,
            CollectorRun.status == STATUS_RUNNING,
        )
        .order_by(CollectorRun.started_at.desc())
    )
    return session.scalars(stmt).first()


def list_runs(
    session: Session, customer_id: str, tenant_id: str, limit: int = 20
) -> list[CollectorRun]:
    stmt = (
        select(CollectorRun)
        .where(CollectorRun.customer_id == customer_id, CollectorRun.tenant_id == tenant_id)
        .order_by(CollectorRun.started_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def _current_runs(customer_id: str, tenant_id: str):
    """
    The latest finished run of each collector: what the last scans could not read.

    Errors from older runs stay in the table with their run but are history;
    counting them would add up the same gap once per scan.
    """
    latest = (
        select(CollectorRun.kind, func.max(CollectorRun.started_at).label("started_at"))
        .where(
            CollectorRun.customer_id == customer_id,
            CollectorRun.tenant_id == tenant_id,
            CollectorRun.status != STATUS_RUNNING,
        )
        .group_by(CollectorRun.kind)
        .subquery()
    )
    return (
        select(CollectorRun.id)
        .join(
            latest,
            (CollectorRun.kind == latest.c.kind)
            & (CollectorRun.started_at == latest.c.started_at),
        )
        .where(
            CollectorRun.customer_id == customer_id,
            CollectorRun.tenant_id == tenant_id,
        )
    )


def list_errors(
    session: Session, customer_id: str, tenant_id: str, limit: int = 200
) -> list[CollectorError]:
    stmt = (
        select(CollectorError)
        .where(
            CollectorError.customer_id == customer_id,
            CollectorError.tenant_id == tenant_id,
            CollectorError.run_id.in_(_current_runs(customer_id, tenant_id)),
        )
        .order_by(CollectorError.occurred_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def count_errors(session: Session, customer_id: str, tenant_id: str) -> int:
    stmt = select(func.count(CollectorError.id)).where(
        CollectorError.customer_id == customer_id,
        CollectorError.tenant_id == tenant_id,
        CollectorError.run_id.in_(_current_runs(customer_id, tenant_id)),
    )
    return session.scalar(stmt) or 0


def reap_orphaned_runs(session: Session) -> int:
    """
    Mark runs left 'running' by a killed process as failed.

    Without this they linger forever and the one-at-a-time rule blocks every
    future run of that kind.
    """
    from .models import ScanPass

    stmt = select(CollectorRun).where(CollectorRun.status == STATUS_RUNNING)
    n = 0
    for run in session.scalars(stmt):
        run.status = STATUS_FAILED
        run.error = "Interrupted — the server stopped before this collector finished."
        run.finished_at = utcnow()
        n += 1
    for scan in session.scalars(select(ScanPass).where(ScanPass.status == STATUS_RUNNING)):
        scan.status = STATUS_FAILED
        scan.outcome = {**(scan.outcome or {}), "interrupted": True}
        scan.finished_at = utcnow()
        n += 1
    if n:
        session.commit()
    return n


def run_age_seconds(run: CollectorRun) -> float | None:
    if run.started_at is None:
        return None
    started = run.started_at
    if started.tzinfo is not None:  # normalise to the naive UTC we store
        started = started.replace(tzinfo=None)
    return (utcnow() - started).total_seconds()
