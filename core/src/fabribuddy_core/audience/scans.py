"""
Running scans against a *stored* connection and persisting the results.

Reads serve the most recent successful `scan_run`, so results survive a
restart. Reads never start a scan: an inventory read with nothing stored
answers 409 and the UI points at Rescan. `run_full_refresh` is what Rescan and
the schedule run — every kind in order, each recorded, none stopping the next.

The licence x activity join at the bottom is carried over from the POC's
main.py unchanged in substance — it is the unused-seat hook and the
`activityKnown` guard matters: a seat is never called unused when there is no
activity to compare it against.
"""

import logging

from sqlalchemy.orm import Session

from ..models import ConnectedTenant
from ..redaction import scrub
from ..services.connections import ConnectionError_, credentials_for
from . import activity_store, repository
from .models import (
    ALL_KINDS,
    KIND_ACTIVITY,
    KIND_LICENSES,
    KIND_PERMISSIONS,
    KIND_SCAN,
    STATUS_FAILED,
    STATUS_OK,
)
from .powerbi import graph, permissions, refresh, scanner

DEFAULT_ACTIVITY_DAYS = 30

log = logging.getLogger("fabribuddy.audience")


def scope_ids(tenant: ConnectedTenant) -> set[str] | None:
    """The Admin page's workspace selection, or None for every workspace."""
    if tenant.scope_mode == "selected":
        return {w.lower() for w in (tenant.selected_workspaces or [])}
    return None


def workspace_cap(session: Session, tenant: ConnectedTenant) -> int | None:
    """Audience's hard cap on scanned workspaces (the trial's 3), or None."""
    from .. import entitlements

    return entitlements.workspace_cap(session, tenant.customer_id)


class ScanFailed(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ScanAlreadyRunning(ScanFailed):
    """A scan of this kind is already in flight — a conflict, not a failure."""


def _execute(session: Session, tenant: ConnectedTenant, kind: str, params: dict, fn):
    """Run `fn(creds)` as a recorded scan_run, persisting success or failure."""
    run = repository.start_run(session, tenant, kind, params)
    secret = None
    try:
        creds = credentials_for(tenant)
        secret = creds.client_secret
        payload = fn(creds)
    except (scanner.ScanError, graph.GraphUnavailable, ConnectionError_) as e:
        message = scrub(str(e), secret) or "Scan failed."
        repository.finish_run(session, run, status=STATUS_FAILED, error=message)
        raise ScanFailed(message) from None  # drop the chain: it can hold creds
    repository.finish_run(session, run, status=STATUS_OK, payload=payload)
    return payload


# --- tenant scan -----------------------------------------------------------
def run_scan(session: Session, tenant: ConnectedTenant) -> dict:
    cap = workspace_cap(session, tenant)

    def work(creds):
        result = scanner.scan_tenant(creds, only_ids=scope_ids(tenant), max_workspaces=cap)
        # Refresh state is a bonus, never a reason to fail the scan: it only
        # covers datasets on dedicated capacity with recent history.
        result["refreshables"] = refresh.fetch_refreshables(creds)
        return result

    return _execute(session, tenant, KIND_SCAN, {}, work)


class NotScanned(ScanFailed):
    """Nothing stored yet. Reads never start a scan — see module docstring."""


def get_scan(session: Session, tenant: ConnectedTenant) -> dict:
    run = repository.latest_run(session, tenant.customer_id, tenant.id, KIND_SCAN)
    if run is not None and run.payload:
        return run.payload
    raise NotScanned("This tenant has not been scanned yet. Run a scan from the top bar.")


# --- activity --------------------------------------------------------------
def _item_to_workspace(session: Session, tenant: ConnectedTenant) -> dict:
    """
    name -> workspace, built from the stored scan.

    The audit log leaves WorkspaceName empty on most events, so without this
    every "last activity" column is blank. Reports win over models on a name
    clash because report names are what activity events usually carry.
    """
    run = repository.latest_run(session, tenant.customer_id, tenant.id, KIND_SCAN)
    if run is None or not run.payload:
        return {}
    mapping = {}
    for m in run.payload.get("models", []):
        if m.get("name"):
            mapping[m["name"]] = m.get("ws", "")
    for r in run.payload.get("reports", []):
        if r.get("name"):
            mapping[r["name"]] = r.get("ws", "")
    return mapping


def run_activity(
    session: Session,
    tenant: ConnectedTenant,
    days: int = DEFAULT_ACTIVITY_DAYS,
    full: bool = False,
) -> dict:
    # One at a time per tenant: concurrent pulls only rate-limit each other.
    inflight = repository.running_run(session, tenant.customer_id, tenant.id, KIND_ACTIVITY)
    if inflight is not None:
        raise ScanAlreadyRunning("An activity scan is already running for this tenant.")
    ws_by_item = _item_to_workspace(session, tenant)

    def work(creds):
        # Incremental: only the days we do not already hold. A warm tenant
        # refreshes in seconds instead of re-reading a million events.
        return activity_store.refresh(
            session, tenant, creds, days=days, ws_by_item=ws_by_item, full=full
        )

    stats = _execute(session, tenant, KIND_ACTIVITY, {"days": days, "full": full}, work)
    return {"live": True, **stats}


def activity_scope(session: Session, tenant: ConnectedTenant) -> dict | None:
    """
    The scanned workspaces and the reports and models in them, when the last
    inventory scan was narrowed (a workspace selection or a plan's cap); None
    when it covered the whole tenant.
    """
    run = repository.latest_run(session, tenant.customer_id, tenant.id, KIND_SCAN)
    payload = (run.payload or {}) if run is not None else {}
    if not payload or not (payload.get("scoped") or payload.get("cappedAt")):
        return None
    return {
        "workspaces": {w["name"] for w in payload.get("workspaces", []) if w.get("name")},
        "items": {
            x["name"]
            for x in (*payload.get("reports", []), *payload.get("models", []))
            if x.get("name")
        },
    }


def get_activity(
    session: Session,
    tenant: ConnectedTenant,
    days: int = DEFAULT_ACTIVITY_DAYS,
    *,
    scoped: bool = True,
) -> dict:
    """
    Serve the stored activity scan. NEVER starts one.

    A 30-day audit-log pull is 7-10 minutes on a busy tenant. Lazy-triggering
    it from a read meant every page load launched another one, several ran
    concurrently against the same API, and none ever finished — so no run was
    ever stored, which triggered yet more. Scanning is now explicit.
    """
    # Aggregates are computed from the stored rows, so changing the window is
    # instant and never re-reads the audit log.
    stored = activity_store.aggregate(
        session,
        tenant,
        days=days,
        scope=activity_scope(session, tenant) if scoped else None,
    )
    if stored.get("live"):
        return stored

    inflight = repository.running_run(session, tenant.customer_id, tenant.id, KIND_ACTIVITY)
    return {
        "live": False,
        "scanned": False,
        "running": inflight is not None,
        "error": (
            "Activity is being scanned now — this takes several minutes on a busy tenant."
            if inflight
            else "Activity has not been scanned yet. Run a scan to populate usage data."
        ),
    }


# --- per-item permissions --------------------------------------------------
def run_permissions(
    session: Session,
    tenant: ConnectedTenant,
    include_personal: bool | None = None,
    max_workspaces: int | None = None,
) -> dict:
    """
    Scan per-item grants. Slower than the other scans (one async round trip per
    100 workspaces), so it is always an explicit action, never lazy-loaded.
    """

    if include_personal is None:
        include_personal = bool(tenant.include_personal)
    only_ids = scope_ids(tenant)
    if workspace_cap(session, tenant) is not None:
        # Capped (trial): exactly the workspaces the inventory scanned, never
        # personal ones — the cap is on the estate the customer sees.
        run = repository.latest_run(session, tenant.customer_id, tenant.id, KIND_SCAN)
        only_ids = {
            w["id"] for w in ((run.payload or {}).get("workspaces", []) if run else [])
        }
        include_personal = False

    def work(creds):
        ids = permissions.list_workspace_ids(
            creds, include_personal=include_personal, only_ids=only_ids
        )
        if max_workspaces:
            ids = ids[:max_workspaces]
        result = permissions.scan_permissions(creds, ids)

        # Expand Entra security groups to the people inside them. Without this
        # a grant to "BE-terna.All Employees" looks like one principal, when it
        # is really everyone — the single biggest way a permission report
        # understates who can see a report.
        group_ids = list((result.get("groupPrincipals") or {}).keys())
        if group_ids:
            try:
                expanded = graph.fetch_group_members(creds, group_ids)
                result["groupMembers"] = expanded["members"]
                result["groupErrors"] = expanded["errors"]
            except graph.GraphUnavailable as e:
                result["groupMembers"] = {}
                result["groupErrors"] = {"*": str(e)}
        else:
            result["groupMembers"] = {}
            result["groupErrors"] = {}
        return result

    return _execute(
        session,
        tenant,
        KIND_PERMISSIONS,
        {"includePersonal": include_personal, "maxWorkspaces": max_workspaces},
        work,
    )


def get_permissions(session: Session, tenant: ConnectedTenant) -> dict | None:
    """The stored permission scan, or None if one has never been run."""
    run = repository.latest_run(session, tenant.customer_id, tenant.id, KIND_PERMISSIONS)
    if run is None or not run.payload:
        return None
    return {
        **run.payload,
        "scannedAt": run.started_at.isoformat() if run.started_at else None,
    }


# --- licences (+ the unused-seat join) -------------------------------------
def run_licenses(
    session: Session, tenant: ConnectedTenant, days: int = DEFAULT_ACTIVITY_DAYS
) -> dict:
    lic = _execute(
        session, tenant, KIND_LICENSES, {}, lambda c: {"rows": graph.fetch_licenses(c)}
    )["rows"]
    return _join_licenses(session, tenant, lic, days)


def get_licenses(
    session: Session,
    tenant: ConnectedTenant,
    days: int = DEFAULT_ACTIVITY_DAYS,
) -> dict:
    """
    Licences are stored raw; the join is recomputed on read so it always
    reflects the latest activity window. Never fetches.
    """
    run = repository.latest_run(session, tenant.customer_id, tenant.id, KIND_LICENSES)
    if run is not None and run.payload:
        return _join_licenses(session, tenant, run.payload.get("rows", []), days)
    return {"live": False, "error": "Licences have not been scanned yet."}


def _join_licenses(session: Session, tenant: ConnectedTenant, lic: list, days: int) -> dict:
    """
    Carried over from the POC's main.py. Flags a seat as unused when its UPN
    appears in no activity event in the window — and never flags anything when
    there is no activity data at all (`activityKnown`).
    """
    active_upns: set[str] = set()
    last_seen: dict = {}
    try:
        # Reads only — a licence request must never launch an activity scan.
        # Tenant-wide on purpose: activity in an unselected workspace still
        # means the seat is used.
        act = get_activity(session, tenant, days=days, scoped=False)
        if act.get("live"):
            last_seen = act.get("lastSeenByUser", {}) or {}
            # activeUsers is computed over EVERY event in the window; the
            # events list is only a sample for the feed, so joining against it
            # would call active people unused.
            active_upns = {u.lower() for u in act.get("activeUsers", []) if u} or {
                (e.get("user") or "").lower() for e in act.get("events", [])
            }
    except ScanFailed:
        # No activity data — licences still list, just without the unused flag.
        pass

    pro = ppu = 0
    rows = []
    for u in lic:
        used = u["upn"] in active_upns if active_upns else None
        if u["license"] == "Pro":
            pro += 1
        elif u["license"] == "PPU":
            ppu += 1
        rows.append(
            {
                "name": u["name"],
                "upn": u["upn"],
                # Any interaction counts here, not just report views — a seat is
                # in use if the person touched Power BI at all.
                "lastSeen": last_seen.get(u["upn"], ""),
                "license": u["license"],
                "unused": (used is False) if active_upns else False,
                "activityKnown": bool(active_upns),
            }
        )

    return {
        "live": True,
        "rows": rows,
        "summary": {
            "pro": pro,
            "ppu": ppu,
            "total": len(rows),
            "unused": sum(1 for r in rows if r["unused"]),
            "activityJoined": bool(active_upns),
        },
    }


# --- the whole pass -----------------------------------------------------------
def step_names(tenant: ConnectedTenant, *, permissions_allowed: bool = True) -> list[str]:
    """The steps a full refresh will run, in order (for progress)."""
    names = ["scan"]
    if permissions_allowed:
        names.append("permissions")
    names.append("activity")
    if (tenant.capabilities or {}).get("graph_licenses", {}).get("available"):
        names.append("licenses")
    return names


def run_full_refresh(
    session: Session,
    tenant: ConnectedTenant,
    *,
    permissions_allowed: bool = True,
    on_step=None,
) -> dict:
    """
    Every scan kind, in the order each needs the last: inventory (activity maps
    item names to workspaces through it), permissions (skipped when the plan
    does not include it), activity (incremental), licences (joined to
    activity). A failing kind is recorded on its run and never stops the rest.
    """
    runners = {
        "scan": lambda: run_scan(session, tenant),
        "permissions": lambda: run_permissions(session, tenant),
        "activity": lambda: run_activity(session, tenant),
        "licenses": lambda: run_licenses(session, tenant),
    }
    steps = [
        (name, runners[name])
        for name in step_names(tenant, permissions_allowed=permissions_allowed)
    ]

    outcome = {}
    for name, step in steps:
        if on_step is not None:
            on_step(name)
        try:
            step()
            outcome[name] = "ok"
        except ScanFailed as e:
            outcome[name] = e.message
            log.warning("%s failed for tenant %s: %s", name, tenant.id, e.message)
    return outcome


def any_running(session: Session, tenant: ConnectedTenant) -> bool:
    return any(
        repository.running_run(session, tenant.customer_id, tenant.id, kind)
        for kind in ALL_KINDS
    )
