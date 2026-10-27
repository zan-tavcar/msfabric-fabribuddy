"""
The lineage API.

Every read goes through `views`, which builds its payload from one Context so a
node page is a handful of queries rather than one per section. No read ever
triggers a collector.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import config, repository
from ..db import get_session
from ..deps import (
    current_customer_id,
    current_user,
    default_tenant,
    get_tenant,
    require_admin,
    require_contributor,
    require_unlocked,
)
from ..lineage import selectors, views
from ..models import ConnectedTenant
from ..schemas import CollectorErrorOut, CollectorRunOut
from ..services import lineage as service

log = logging.getLogger("lineage.routes")

router = APIRouter(prefix="/api/lineage", tags=["lineage"])


@router.get("/tree")
def tree(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """
    Sidebar hierarchy: workspace -> lakehouse -> schema.table | model | report,
    plus the External sources group. Never triggers a collection.
    """
    return service.get_tree(session, tenant)


@router.get("/status")
def status(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return {
        "tenant": {
            "id": tenant.id,
            "displayName": tenant.display_name,
            "isDemo": bool(tenant.is_demo),
            "capabilities": tenant.capabilities or {},
        },
        **service.collection_status(session, tenant),
    }


@router.post("/scan", dependencies=[Depends(require_admin)])
def scan(
    tenant: ConnectedTenant = Depends(default_tenant),
    wait: bool = Query(False, description="Run inline instead of queueing"),
    session: Session = Depends(get_session),
):
    """
    Kept for scripts that used it: now the same pass as POST /api/admin/scan —
    both products, incremental, recorded in the scan history.
    """
    from .admin import start_pass

    return start_pass(session, tenant, full=False, wait=wait, trigger="api")


@router.post("/refresh", dependencies=[Depends(require_admin)])
def refresh(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """Kept for scripts that used it: a full pass of both products, queued."""
    from .admin import start_pass

    return start_pass(session, tenant, full=True, wait=False, trigger="api")


@router.get("/runs", response_model=list[CollectorRunOut])
def runs(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    customer_id: str = Depends(current_customer_id),
    limit: int = Query(20, ge=1, le=100),
):
    return [
        CollectorRunOut.of(r)
        for r in repository.list_runs(session, customer_id, tenant.id, limit)
    ]


@router.get("/errors", response_model=list[CollectorErrorOut])
def errors(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    customer_id: str = Depends(current_customer_id),
    limit: int = Query(200, ge=1, le=1000),
):
    """
    Per-item collection failures.

    Surfaced as a first-class endpoint because partial failure is the normal
    state of a real tenant — a protected notebook here, a workspace without
    Contributor there — and hiding it would make the inventory look complete
    when it is not.
    """
    return [
        CollectorErrorOut.of(e)
        for e in repository.list_errors(session, customer_id, tenant.id, limit)
    ]


def _context(tenant: ConnectedTenant, session: Session) -> views.Context:
    return views.Context(session, tenant.customer_id, tenant.id)


@router.get("/overview")
def overview(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    workspace: str = Query("", description="Workspace id (ws:…) to scope to"),
    item: str = Query("", description="Item id (item:…) to scope to"),
    table: str = Query("", description="Node id to scope to"),
    q: str = Query("", description="Search: table name, schema.table or path"),
):
    """Metric cards, open findings and the recently-written list, optionally scoped."""
    return views.overview(
        _context(tenant, session), workspace=workspace, item=item, table=table, q=q
    )


@router.get("/catalog")
def catalog(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    workspace: str = Query(""),
    item: str = Query(""),
    table: str = Query(""),
    q: str = Query(""),
):
    """Every storage table, semantic model and report, with producers and consumers."""
    return views.catalog(
        _context(tenant, session), workspace=workspace, item=item, table=table, q=q
    )


@router.get("/catalog.csv", dependencies=[Depends(require_unlocked)])
def catalog_csv(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    workspace: str = Query(""),
    item: str = Query(""),
    table: str = Query(""),
    q: str = Query(""),
):
    """The catalog as CSV, with the same filters as the Docs page."""
    import csv
    import io

    from fastapi.responses import Response

    rows = views.catalog(
        _context(tenant, session), workspace=workspace, item=item, table=table, q=q
    )["rows"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Workspace",
            "Item",
            "Item type",
            "Schema",
            "Name",
            "Kind",
            "Written by",
            "Producers",
            "Write mode",
            "Last write (UTC)",
            "Rows",
            "Size (bytes)",
            "Semantic models",
            "Reports",
            "Documented",
            "Id",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                _cell(r["workspace"]),
                _cell(r["item"]),
                r["itemType"],
                _cell(r["schema"]),
                _cell(r["name"]),
                r["kindLabel"],
                r["producerKind"],
                _cell("; ".join(f"{p['kind']}: {p['item']}" for p in r["producers"])),
                (r["producers"][0]["mode"] if r["producers"] else ""),
                r["lastWriteAt"] or "",
                r["rows"] if r["rows"] is not None else "",
                r["sizeBytes"] or "",
                r["models"],
                r["reports"],
                "yes" if r["documented"] else "no",
                r["id"],
            ]
        )
    # BOM so Excel opens UTF-8 names (č, ž, …) correctly.
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="fabric-catalog.csv"'},
    )


def _cell(value: str) -> str:
    """Neutralise spreadsheet formulas: a table named `=cmd|…` must stay text."""
    value = value or ""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@router.get("/graph", dependencies=[Depends(require_unlocked)])
def graph(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    focus: str | None = Query(None, description="Node id to centre on"),
    up: int = Query(2, ge=0, le=25),
    down: int = Query(2, ge=0, le=25),
    select: str = Query("", description="dbt-style selector, e.g. +fact_sales+"),
    workspace: str = Query("", description="Workspace id (ws:…) to scope to"),
    item: str = Query("", description="Item id (item:…) to scope to"),
    table: str = Query("", description="Node id to scope to"),
    q: str = Query("", description="Search: table name, schema.table or path"),
):
    """
    Nodes and edges to render.

    With `focus`, returns its neighbourhood and flags the full path through it so
    the UI can highlight and dim. With `select`, returns what the selector
    matched. With neither, the whole estate.
    """
    try:
        return views.graph(
            _context(tenant, session),
            focus=focus,
            up=up,
            down=down,
            select_expr=select,
            workspace=workspace,
            item=item,
            table=table,
            q=q,
        )
    except selectors.SelectorError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.get("/nodes/{node_id}", dependencies=[Depends(require_unlocked)])
def node(
    node_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    payload = views.node_page(_context(tenant, session), node_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Node not found.")
    return payload


@router.get("/nodes/{node_id}/columns", dependencies=[Depends(require_unlocked)])
def node_columns(
    node_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    context = _context(tenant, session)
    if node_id not in context.nodes:
        raise HTTPException(status_code=404, detail="Node not found.")
    return {"columns": views.node_columns(context, node_id)}


class DescriptionIn(BaseModel):
    # Empty clears your description, and the collected one (TMDL, a Delta
    # table property) shows again.
    description: str = Field(default="", max_length=4000)


def _editor(request: Request, session: Session) -> str:
    """Who is editing, as the "last modified by" line shows them."""
    if not config.AUTH_REQUIRED:
        return "Local operator"
    user = current_user(request, session)
    return user.display_name or user.email


@router.put("/nodes/{node_id}/description", dependencies=[Depends(require_contributor)])
def set_node_description(
    node_id: str,
    body: DescriptionIn,
    request: Request,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """
    A table's description, written in FabriBuddy by a contributor or admin.

    Stored here, never in the tenant (the product is read-only); it takes
    precedence over a description collected from TMDL or Delta properties.
    """
    context = _context(tenant, session)
    if node_id not in context.nodes:
        raise HTTPException(status_code=404, detail="Node not found.")
    views.set_description(
        context,
        node_id=node_id,
        column=None,
        text=body.description,
        editor=_editor(request, session),
    )
    return views.node_page(_context(tenant, session), node_id)


@router.put(
    "/nodes/{node_id}/columns/{column}/description",
    dependencies=[Depends(require_contributor)],
)
def set_column_description(
    node_id: str,
    column: str,
    body: DescriptionIn,
    request: Request,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """A column's description, written in FabriBuddy (see the table route)."""
    context = _context(tenant, session)
    if node_id not in context.nodes:
        raise HTTPException(status_code=404, detail="Node not found.")
    if not views.set_description(
        context,
        node_id=node_id,
        column=column,
        text=body.description,
        editor=_editor(request, session),
    ):
        raise HTTPException(status_code=404, detail="Column not found.")
    return {"columns": views.node_columns(_context(tenant, session), node_id)}


@router.get("/nodes/{node_id}/code", dependencies=[Depends(require_unlocked)])
def node_code(
    node_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """
    The defining artifact. Returns `available: false` with a specific reason
    rather than a 404 — "not Contributor on this workspace" is an answer.
    """
    return views.node_code(_context(tenant, session), node_id)


@router.get("/items/{item_id}", dependencies=[Depends(require_unlocked)])
def item_page(
    item_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    """A notebook, pipeline or dataflow on its own: its code, writes, reads, warnings."""
    page = views.item_page(_context(tenant, session), item_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Item not found.")
    return page


@router.get("/nodes/{node_id}/access", dependencies=[Depends(require_unlocked)])
def node_access(
    node_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    context = _context(tenant, session)
    if node_id not in context.nodes:
        raise HTTPException(status_code=404, detail="Node not found.")
    return views.node_access(context, node_id)


@router.get("/columns/{column_id}/trace", dependencies=[Depends(require_unlocked)])
def column_trace(
    column_id: str,
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    payload = views.column_trace(_context(tenant, session), column_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Column not found.")
    return payload


@router.get("/search", dependencies=[Depends(require_unlocked)])
def search(
    q: str = Query("", description="At least two characters"),
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
):
    return views.search(_context(tenant, session), q)


@router.get("/findings")
def findings(
    tenant: ConnectedTenant = Depends(default_tenant),
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
):
    return {"findings": views.open_findings(_context(tenant, session), limit)}


# Tenant-scoped variants, for when more than one tenant is connected.
scoped = APIRouter(prefix="/api/tenants/{tenant_id}/lineage", tags=["lineage"])


@scoped.get("/tree")
def tree_scoped(
    tenant: ConnectedTenant = Depends(get_tenant),
    session: Session = Depends(get_session),
):
    return service.get_tree(session, tenant)


@scoped.get("/status")
def status_scoped(
    tenant: ConnectedTenant = Depends(get_tenant),
    session: Session = Depends(get_session),
):
    return service.collection_status(session, tenant)
