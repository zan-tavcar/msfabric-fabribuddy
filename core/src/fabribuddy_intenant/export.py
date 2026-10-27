"""
Every answer the Fabric App will ask for, computed once after a scan.

The app is the SaaS frontend; it asks the same routes the SaaS backend serves.
This module asks them too — in-process, through FastAPI's test client, so the
answers are byte-for-byte what the SaaS would send — and returns them keyed by
request. Nothing here re-implements a page: `views.py` and the routers do the
work, exactly as in production.

What is stored:

- the shell: auth config (marked `inTenant`), subscription (fixed: see
  api.py), scan progress;
- Data Catalog: tree, status, overview, catalog, gaps, the whole graph; and
  per table / model / report: its page, columns, code, access, its mini graph
  and its "View in lineage" graph; per notebook / pipeline / storage item: its page;
- Report Audience: status, summary, workspaces, reports, models, people, runs,
  refresh state, 30 days of activity and licences, permissions.

Filtered variants (the filter bar) are not stored; the in-tenant app hides it.
"""

import logging
import time
from collections.abc import Callable
from urllib.parse import quote

log = logging.getLogger("fabribuddy.intenant.export")

# Sidebar items that open an item page (frontend: lib/tree.ts ITEM_PAGE_KINDS).
ITEM_PAGE_TYPES = {
    "Lakehouse",
    "Warehouse",
    "MirroredDatabase",
    "SQLDatabase",
    "Notebook",
    "DataPipeline",
    "Dataflow",
    "SparkJobDefinition",
    "CopyJob",
    "Eventstream",
    "VariableLibrary",
}
# A stored error answer: {ERROR_KEY: <status>, "detail": <message>}
# (frontend: app/src/fabric/transport.ts).
ERROR_KEY = "__fabribuddyError"
# Report Audience's activity and licence window (frontend: AudienceApp DAYS).
AUDIENCE_DAYS = 30

STATIC = (
    "/scan/progress",
    "/lineage/tree",
    "/lineage/status",
    "/lineage/overview",
    "/lineage/catalog",
    "/lineage/errors",
    "/lineage/graph",
    "/audience/status",
    "/audience/summary",
    "/audience/workspaces",
    "/audience/reports",
    "/audience/models",
    "/audience/users",
    "/audience/runs?limit=10",
    "/audience/refreshables",
    f"/audience/activity?days={AUDIENCE_DAYS}",
    f"/audience/licenses?days={AUDIENCE_DAYS}",
    "/audience/permissions",
)

def _seg(value: str) -> str:
    return quote(value, safe="")


def _selector(node_id: str) -> str:
    """The graph selector "View in lineage" sends (frontend: lib/catalog.ts lineageOf)."""
    quoted = f'"{node_id}"' if any(c.isspace() for c in node_id) else node_id
    return f"+id:{quoted}+"


def urls(node_ids: list[str], item_ids: list[str]) -> list[str]:
    out = list(STATIC)
    for node_id in node_ids:
        seg = _seg(node_id)
        out += [
            f"/lineage/nodes/{seg}",
            f"/lineage/nodes/{seg}/columns",
            f"/lineage/nodes/{seg}/code",
            f"/lineage/nodes/{seg}/access",
            f"/lineage/graph?focus={seg}&up=25&down=25",
            f"/lineage/graph?select={_seg(_selector(node_id))}",
        ]
    out += [f"/lineage/items/{_seg(item_id)}" for item_id in item_ids]
    return out


def collect(
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, object], dict]:
    """
    Ask every route; return ({url: answer}, stats).

    Must run after `fabribuddy_core` is configured (the runner does this).
    """
    from fastapi.testclient import TestClient

    from fabribuddy_core import config, db, repository
    from fabribuddy_core.lineage import repository as lineage_repository
    from fabribuddy_core.lineage import views
    from fabribuddy_core.routers import lineage as lineage_router

    from . import api

    app = api.build()

    session = db.SessionLocal()
    # One session and one loaded estate for thousands of reads: each route
    # would otherwise reload every node, item and edge on every call.
    contexts: dict[str, views.Context] = {}

    def shared_context(tenant, _session):
        if tenant.id not in contexts:
            contexts[tenant.id] = views.Context(session, tenant.customer_id, tenant.id)
        return contexts[tenant.id]

    def shared_session():
        yield session

    original_context = lineage_router._context
    lineage_router._context = shared_context
    app.dependency_overrides[db.get_session] = shared_session
    try:
        tenants = repository.list_tenants(session, config.DEFAULT_CUSTOMER_ID)
        node_ids: list[str] = []
        item_ids: list[str] = []
        if tenants:
            tenant = tenants[0]
            node_ids = [
                n.id
                for n in lineage_repository.list_nodes(
                    session, tenant.customer_id, tenant.id
                )
            ]
            item_ids = [
                i.id
                for i in lineage_repository.list_items(session, tenant.customer_id, tenant.id)
                if i.item_type in ITEM_PAGE_TYPES
            ]
        wanted = urls(node_ids, item_ids)
        answers: dict[str, object] = {
            "/auth/config": api.AUTH_CONFIG,
            "/auth/subscription": api.SUBSCRIPTION,
        }
        missing: dict[int, int] = {}
        started = time.monotonic()
        client = TestClient(app)
        for n, url in enumerate(wanted, 1):
            response = client.get(f"/api{url}")
            if response.status_code == 200:
                answers[url] = response.json()
            else:
                # Stored too: the app raises the same error the SaaS backend
                # would (a 409 "not scanned", a 404 "no code for this table").
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = None
                answers[url] = {ERROR_KEY: response.status_code, "detail": detail}
                missing[response.status_code] = missing.get(response.status_code, 0) + 1
                log.debug("GET %s -> %s", url, response.status_code)
            if on_progress and (n % 200 == 0 or n == len(wanted)):
                on_progress(n, len(wanted))
        stats = {
            "requests": len(wanted),
            "stored": len(answers) - sum(missing.values()),
            "errors": missing,
            "nodes": len(node_ids),
            "items": len(item_ids),
            "seconds": round(time.monotonic() - started, 1),
        }
        return answers, stats
    finally:
        lineage_router._context = original_context
        app.dependency_overrides.pop(db.get_session, None)
        session.close()
