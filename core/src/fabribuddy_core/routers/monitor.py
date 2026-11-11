"""
The Job Monitor API: runnable items, their runs, schedules and chains.

Reads only what the jobs collector stored; never calls Fabric.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import default_tenant, require_unlocked
from ..lineage import monitor
from ..models import ConnectedTenant
from . import lineage as lineage_router

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


def _context(tenant: ConnectedTenant, session: Session):
    # The lineage router's: the in-tenant export swaps it for one shared context.
    return lineage_router._context(tenant, session)


@router.get("/jobs")
def jobs(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Every runnable item: schedule, next run, latest run, recent runs, chain neighbours."""
    return monitor.jobs(_context(tenant, session))


@router.get("/runs", dependencies=[Depends(require_unlocked)])
def runs(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Every run across items, newest first."""
    return monitor.runs(_context(tenant, session))


@router.get("/jobs/{item_id}", dependencies=[Depends(require_unlocked)])
def job(
    item_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """One item: its runs with what each executed, its chain, its code."""
    page = monitor.job(_context(tenant, session), item_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Not a runnable item.")
    return page


@router.get("/live")
def live(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Running now, just finished, up next — small, so the app can poll it."""
    return monitor.live(_context(tenant, session))
