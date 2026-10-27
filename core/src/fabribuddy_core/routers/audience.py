"""
The audience API: what the scans found, read from the database.

Every route resolves the signed-in organisation's tenant (`default_tenant`) and
reads the latest stored scan. No read ever starts a scan — an audit-log pull is
minutes, and a page load must be milliseconds. Rescan (`POST /api/admin/scan`)
and the schedule are the only things that talk to Microsoft.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..audience import repository, scans
from ..audience.models import (
    KIND_ACTIVITY,
    KIND_LICENSES,
    KIND_PERMISSIONS,
    KIND_SCAN,
)
from ..db import get_session
from ..deps import default_tenant, require_unlocked
from ..models import ConnectedTenant
from ..schemas import ScanRunOut

router = APIRouter(prefix="/api/audience", tags=["audience"])

SCAN_KINDS = {
    KIND_SCAN: "Tenant inventory",
    KIND_PERMISSIONS: "Permissions & Entra groups",
    KIND_ACTIVITY: "Usage activity",
    KIND_LICENSES: "Licences",
}


def _scan(session: Session, tenant: ConnectedTenant) -> dict:
    try:
        return scans.get_scan(session, tenant)
    except scans.NotScanned as e:
        raise HTTPException(status_code=409, detail=e.message) from None


@router.get("/status")
def status(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Every scan kind's state — what the UI polls while a scan runs."""
    out = []
    for kind, label in SCAN_KINDS.items():
        running = repository.running_run(session, tenant.customer_id, tenant.id, kind)
        last_ok = repository.latest_run(session, tenant.customer_id, tenant.id, kind)
        last_any = repository.latest_run(
            session, tenant.customer_id, tenant.id, kind, only_ok=False
        )
        out.append(
            {
                "kind": kind,
                "label": label,
                "running": running is not None,
                "startedAt": running.started_at.isoformat() if running else None,
                "lastSuccessAt": last_ok.started_at.isoformat() if last_ok else None,
                "lastRunAt": last_any.started_at.isoformat() if last_any else None,
                "lastStatus": last_any.status if last_any else None,
                "lastError": last_any.error if last_any else None,
            }
        )
    return {
        "tenant": {
            "id": tenant.id,
            "displayName": tenant.display_name,
            "isDemo": bool(tenant.is_demo),
            "capabilities": tenant.capabilities or {},
        },
        "scans": out,
        "anyRunning": any(s["running"] for s in out),
    }


@router.get("/summary")
def summary(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return _scan(session, tenant).get("summary", {})


@router.get("/workspaces")
def workspaces(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return _scan(session, tenant).get("workspaces", [])


@router.get("/reports")
def reports(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return _scan(session, tenant).get("reports", [])


@router.get("/models")
def models(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return _scan(session, tenant).get("models", [])


@router.get("/users")
def users(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return _scan(session, tenant).get("users", [])


@router.get("/refreshables")
def refreshables(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Semantic-model refresh state captured with the last inventory scan."""
    return _scan(session, tenant).get("refreshables", {"available": False, "items": {}})


@router.get("/activity")
def activity(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    days: int = Query(30, ge=1, le=30),
):
    return scans.get_activity(session, tenant, days=days)


@router.get("/licenses")
def licenses(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    days: int = Query(30, ge=1, le=30),
):
    return scans.get_licenses(session, tenant, days=days)


@router.get("/permissions", dependencies=[Depends(require_unlocked)])
def permissions(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Stored per-item grants and group expansion, or `scanned: false`."""
    data = scans.get_permissions(session, tenant)
    if data is None:
        return {"scanned": False}
    return {"scanned": True, **data}


@router.get("/runs", response_model=list[ScanRunOut])
def runs(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    limit: int = Query(20, ge=1, le=100),
):
    return [
        ScanRunOut.of(r)
        for r in repository.list_runs(session, tenant.customer_id, tenant.id, limit)
    ]
