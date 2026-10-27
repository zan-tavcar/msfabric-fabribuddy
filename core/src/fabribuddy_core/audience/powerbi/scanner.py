"""
FabriBuddy local scanner — calls the real Power BI Admin API against your
tenant using a service principal, and normalises the results into the shape
the frontend expects.

Credentials are passed in explicitly (see credentials.py) so one process can
scan several connected tenants. They are resolved from the secret store by
the caller and never hardcoded or written to disk here.

Two scan depths:
  scan_tenant()  — fast: /admin/groups with $expand (workspaces, reports,
                   datasets, users + roles).
  enrich_detail()— uses the scanner APIs (getInfo) to add dataset-level
                   detail where available. Best-effort; degrades gracefully
                   if the tenant/permissions don't allow it.

Anything the admin API cannot give us (Pro/PPU license usage, resolved
group membership, activity events, RLS role membership, app audiences)
is NOT faked here — the backend reports it as unavailable and the frontend
shows clearly-labelled sample data for those panels.
"""

import datetime as _dt
import os
import time

import requests

from .credentials import Credentials

try:
    import msal
except ImportError as e:
    raise SystemExit("Missing dependency. Run: pip install -r requirements.txt") from e

AUTHORITY_BASE = "https://login.microsoftonline.com"
SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
API = "https://api.powerbi.com/v1.0/myorg"


class ScanError(Exception):
    pass


def norm_id(value) -> str:
    """
    Lowercase a Power BI object id.

    /admin/groups returns GUIDs upper-cased; the scanner API returns them
    lower-cased. Comparing them raw meant zero of 564 reports matched between
    the two scans, so every drill-through into permissions silently failed.
    """
    return (value or "").strip().lower()


def credentials_from_env():
    """Bootstrap path: the same three env vars the POC used."""
    tid = os.environ.get("PA_TENANT_ID")
    cid = os.environ.get("PA_CLIENT_ID")
    sec = os.environ.get("PA_CLIENT_SECRET")
    if not all([tid, cid, sec]):
        raise ScanError(
            "Set PA_TENANT_ID, PA_CLIENT_ID and PA_CLIENT_SECRET in the "
            "environment before starting the backend."
        )
    return Credentials(tenant_id=tid, client_id=cid, client_secret=sec)


def get_token(creds: Credentials):
    try:
        app = msal.ConfidentialClientApplication(
            creds.client_id,
            authority=f"{AUTHORITY_BASE}/{creds.tenant_id}",
            client_credential=creds.client_secret,
        )
    except ValueError as e:
        # MSAL resolves the authority at construction: an unknown tenant id
        # fails here, before any token request. Common when connecting a
        # tenant by hand, so give it a clean message instead of a 500.
        raise ScanError(
            f"Could not reach the sign-in authority for tenant "
            f"'{creds.tenant_id}'. Check the TenantId is correct. ({str(e)[:200]})"
        ) from None
    except requests.RequestException as e:
        raise ScanError(f"Network error contacting Microsoft sign-in: {e}") from None

    try:
        result = app.acquire_token_for_client(scopes=SCOPE)
    except requests.RequestException as e:
        raise ScanError(f"Network error acquiring a token: {e}") from None
    if "access_token" not in result:
        raise ScanError(
            f"Token acquisition failed: {result.get('error')} — "
            f"{result.get('error_description', '')[:200]}"
        )
    return result["access_token"]


# A 30-day activity pull is thousands of sequential requests over many
# minutes, and Microsoft will reset the connection partway through. requests
# raises ConnectionError for that, which is NOT an HTTP status and was
# escaping every handler as an unhandled 500 — discarding the whole scan.
NETWORK_RETRIES = 4


def _get(token, path, params=None, _attempt=1):
    try:
        r = requests.get(
            f"{API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=90,
        )
    except requests.RequestException as e:
        if _attempt <= NETWORK_RETRIES:
            time.sleep(min(2**_attempt, 20))
            return _get(token, path, params, _attempt + 1)
        raise ScanError(
            f"Network error calling {path} after {NETWORK_RETRIES} retries: "
            f"{type(e).__name__}: {str(e)[:160]}"
        ) from None
    if r.status_code in (401, 403):
        raise ScanError(
            "Admin API access denied. Confirm in the Power BI/Fabric admin "
            "portal that 'Allow service principals to use read-only admin "
            "APIs' is ON and this SPN's security group is added to it."
        )
    if r.status_code == 429:
        wait = int(r.headers.get("Retry-After", "10"))
        time.sleep(wait)
        return _get(token, path, params, _attempt)
    if r.status_code >= 500 and _attempt <= NETWORK_RETRIES:
        # Microsoft returns transient 500s mid-scan (PowerBIHttpRequestException).
        # One of them used to truncate a 30-day window to a single day.
        time.sleep(min(2**_attempt, 20))
        return _get(token, path, params, _attempt + 1)
    if r.status_code != 200:
        raise ScanError(f"{path} returned {r.status_code}: {r.text[:300]}")
    return r.json()


def validate(token):
    _get(token, "/admin/groups", {"$top": 1})
    return True


# /admin/groups caps $top at 5000 and needs $skip to page. The POC passed a
# single $top=1000 and silently truncated on any tenant larger than that —
# every count and every percentage derived from it was an undercount.
GROUPS_PAGE_MAX = 5000


def _all_groups(token, page_size=1000, hard_cap=100_000):
    """Page through /admin/groups until a short page comes back."""
    page_size = min(max(page_size, 1), GROUPS_PAGE_MAX)
    out, skip = [], 0
    while len(out) < hard_cap:
        page = _get(
            token,
            "/admin/groups",
            {
                "$top": page_size,
                "$skip": skip,
                "$expand": "users,reports,dashboards,datasets",
            },
        ).get("value", [])
        out.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return out


# Direct Lake is NOT exposed by the admin scanner API: a Direct Lake model
# reports targetStorageMode "PremiumFiles", identical to a large import model,
# and enabling datasetSchema/datasetExpressions yields no partition-mode hint
# either (verified against a tenant that has them). Rather than guess, storage
# mode is reported from what the API does say, and the UI states the limit.
STORAGE_LABELS = {
    "pbixinimportmode": "Import",
    "inimportmode": "Import",
    "pbixindirectquerymode": "DirectQuery",
    "pbixincompositemode": "Composite",
    "usagemetricsuserreport": "Usage metrics",
}


def is_personal_workspace(group) -> bool:
    """
    Power BI does not always report personal workspaces as type
    PersonalGroup — on many tenants they come back as ordinary Workspaces
    named "PersonalWorkspace: <person>". The name convention is the reliable
    signal, so both are accepted.
    """
    if group.get("type") == "PersonalGroup":
        return True
    return (group.get("name") or "").strip().lower().startswith("personalworkspace")


def classify_workspace(group, sku_by_capacity):
    """
    Fabric / PPU / Premium / Pro, from the capacity the workspace sits on.

    SKU prefixes are the only reliable signal: F* is Fabric, PP* is Premium
    Per User, P/EM/A are classic Premium/embedded, and no dedicated capacity
    means the workspace runs on shared (Pro) capacity.
    """
    if is_personal_workspace(group):
        return "Personal"
    sku = (sku_by_capacity.get(group.get("capacityId") or "") or "").upper()
    if not sku:
        return "Pro"
    if sku.startswith("PP"):
        return "PPU"
    if sku.startswith("F"):
        return "Fabric"
    if sku[0] in ("P", "A") or sku.startswith("EM"):
        return "Premium"
    return "Pro"


def fetch_capacities(token):
    """capacityId -> SKU. Best effort: a tenant may expose no capacities."""
    try:
        # /admin/capacities rejects $top outright ("Query option 'Top' is not
        # allowed"), unlike every other admin endpoint.
        data = _get(token, "/admin/capacities")
    except ScanError:
        return {}
    return {c.get("id"): (c.get("sku") or "") for c in data.get("value", []) if c.get("id")}


def scan_tenant(
    creds: Credentials,
    top=1000,
    only_ids: set[str] | None = None,
    max_workspaces: int | None = None,
):
    """
    Enumerate workspaces with their users, reports and datasets via
    /admin/groups. Returns everything the frontend needs for the live tabs,
    plus flags marking which richer datasets are unavailable from this scan.

    `only_ids` narrows the scan to the Admin page's workspace selection
    (lower-cased GUIDs); `max_workspaces` then caps it (the trial), keeping the
    first by name so the choice is stable between scans. `available` always
    lists every shared workspace, so the selection can be edited without a
    second call.
    """
    token = get_token(creds)
    validate(token)

    groups = _all_groups(token, page_size=top)
    sku_by_capacity = fetch_capacities(token)

    if max_workspaces is not None:
        eligible = sorted(
            (
                g
                for g in groups
                if not is_personal_workspace(g)
                and (only_ids is None or norm_id(g.get("id")) in only_ids)
            ),
            key=lambda g: (g.get("name") or "").lower(),
        )
        only_ids = {norm_id(g.get("id")) for g in eligible[:max_workspaces]}

    workspaces, reports, models, users_map = [], [], [], {}
    dataset_index = {}  # id -> name, for mapping report.datasetId to a name

    skipped_personal = 0
    available = []
    for g in groups:
        # Personal workspaces are excluded outright: they are one-person
        # scratch space, they distort every count, and they are not what a
        # governance review is about.
        if is_personal_workspace(g):
            skipped_personal += 1
            continue
        available.append(
            {
                "id": norm_id(g.get("id")),
                "name": g.get("name", "(unnamed)"),
                "type": g.get("type", ""),
                "reports": len(g.get("reports", []) or []),
                "models": len(g.get("datasets", []) or []),
            }
        )
        if only_ids is not None and norm_id(g.get("id")) not in only_ids:
            continue
        gname = g.get("name", "(unnamed)")
        gtype = g.get("type", "")
        gusers = g.get("users", []) or []
        greports = g.get("reports", []) or []
        gdatasets = g.get("datasets", []) or []

        for d in gdatasets:
            dataset_index[norm_id(d.get("id"))] = d.get("name")

        workspaces.append(
            {
                "id": norm_id(g.get("id")),
                "name": gname,
                "type": gtype,
                "state": g.get("state", ""),
                "isPersonal": is_personal_workspace(g),
                "capacityId": g.get("capacityId", ""),
                "onDedicatedCapacity": bool(g.get("isOnDedicatedCapacity")),
                "kind": classify_workspace(g, sku_by_capacity),
                "models": [d.get("name") for d in gdatasets],
                "reports": [r.get("name") for r in greports],
                "users": [u.get("displayName") or u.get("identifier") for u in gusers],
                "roles": {
                    (u.get("displayName") or u.get("identifier")): u.get(
                        "groupUserAccessRight", ""
                    )
                    for u in gusers
                },
            }
        )

        for r in greports:
            reports.append(
                {
                    "id": norm_id(r.get("id")),
                    "name": r.get("name"),
                    "ws": gname,
                    "wsType": gtype,
                    "datasetId": norm_id(r.get("datasetId")),
                    "webUrl": r.get("webUrl", ""),
                }
            )

        for d in gdatasets:
            models.append(
                {
                    "id": norm_id(d.get("id")),
                    "name": d.get("name"),
                    "ws": gname,
                    "wsType": gtype,
                    "configuredBy": d.get("configuredBy", ""),
                    "isRefreshable": d.get("isRefreshable"),
                }
            )

        for u in gusers:
            key = u.get("displayName") or u.get("identifier")
            if not key:
                continue
            if key not in users_map:
                users_map[key] = {
                    "name": key,
                    "identifier": u.get("identifier", ""),
                    "principalType": u.get("principalType", ""),
                    "workspaces": [],
                    "models": set(),
                    "reports": set(),
                }
            users_map[key]["workspaces"].append([gname, u.get("groupUserAccessRight", "")])
            # a workspace member can reach that workspace's content
            for d in gdatasets:
                users_map[key]["models"].add(d.get("name"))
            for r in greports:
                users_map[key]["reports"].add(r.get("name"))

    # attach a friendly model name to each report
    for r in reports:
        r["model"] = dataset_index.get(r["datasetId"], "")

    users = []
    for u in users_map.values():
        u["models"] = sorted(x for x in u["models"] if x)
        u["reports"] = sorted(x for x in u["reports"] if x)
        users.append(u)

    shared_ws = workspaces

    return {
        "summary": {
            "workspaces": len(workspaces),
            "sharedWorkspaces": len(shared_ws),
            "personalWorkspaces": skipped_personal,
            "reports": len(reports),
            "models": len(models),
            "users": len(users),
        },
        "workspaces": workspaces,
        "available": available,
        "scoped": only_ids is not None,
        "cappedAt": max_workspaces,
        "reports": reports,
        "models": models,
        "users": users,
        # honesty flags: what this scan level cannot provide
        "unavailable": {
            "licenses": "Needs Microsoft Graph (user license assignments) — not in this scan.",
            "canView": "Needs report+dataset permission resolution + Graph group expansion.",
            "rls": "Needs XMLA endpoint / AS client library — not in the admin REST scan.",
            "activity": "Needs GetActivityEvents (audit log) — not in this scan.",
            "apps": "Needs the Apps admin API + audience resolution — not in this scan.",
        },
    }


# --- activity events -------------------------------------------------------

# Audit-log activities a PERSON performs in the browser. Everything else —
# GetGroupsAsAdmin, GetDatasourceDetailsWithCredentialsAsync and friends — is
# API/service-principal traffic (including FabriBuddy's own scans) and would
# swamp any "is anyone actually using Power BI" trend. Kept as an allowlist so
# an unknown new activity is treated as service noise rather than silently
# inflating usage.
HUMAN_ACTIONS = {
    "ViewReport",
    "ViewDashboard",
    "ViewTile",
    "ViewUsageMetrics",
    "ExportReport",
    "ExportTile",
    "ExportVisualData",
    "ExportDashboard",
    "PrintReport",
    "PrintDashboard",
    "CreateReport",
    "EditReport",
    "DeleteReport",
    "RenameReport",
    "CreateDashboard",
    "EditDashboard",
    "DeleteDashboard",
    "ShareReport",
    "ShareDashboard",
    "ShareDataset",
    "PublishToWebReport",
    "DownloadReport",
    "CreateApp",
    "UpdateApp",
    "InstallApp",
    "ViewApp",
    "AnalyzedByExternalApplication",
    "CreateDataset",
    "EditDataset",
    "DeleteDataset",
}


def is_human_action(action: str) -> bool:
    return (action or "") in HUMAN_ACTIONS


# GetActivityEvents supports a server-side $filter on Activity. Asking only
# for ViewReport turns a quarter-million-event fetch into a few thousand: on a
# The audit log is fetched WITHOUT an Activity filter: licence "last seen" and
# workspace "last activity" must reflect any interaction, not just report
# views. ViewReport-specific figures are derived as a subset of the same pass,
# so one fetch serves both.
#
# Aggregation is incremental — counters are updated per event and only a small
# sample of raw events is retained. That keeps memory flat over a 30-day window
# on a tenant producing hundreds of thousands of events, which is what forced
# the old hard cap (and silently truncated the window).
VIEW_ACTION = "ViewReport"


def fetch_activity_rows(creds: Credentials, start_day, end_day, ws_by_item=None):
    """
    Raw audit rows for a date range, for the incremental store.

    Returns rows plus the days that refused, so the caller can tell a transient
    failure apart from the retention edge rather than assuming silence.
    """
    token = get_token(creds)
    ws_by_item = {k.lower(): v for k, v in (ws_by_item or {}).items()}
    rows: list[dict] = []
    failed_days: list[str] = []
    consecutive = 0

    day = end_day
    while day >= start_day:
        start = f"{day.isoformat()}T00:00:00.000Z"
        end = f"{day.isoformat()}T23:59:59.999Z"
        cont = None
        try:
            while True:
                if cont:
                    params = {"continuationToken": f"'{cont}'"}
                else:
                    params = {"startDateTime": f"'{start}'", "endDateTime": f"'{end}'"}
                data = _get(token, "/admin/activityevents", params)
                for ev in data.get("activityEventEntities", []):
                    ts = ev.get("CreationTime") or ""
                    if not ts:
                        continue
                    item = ev.get("ReportName") or ev.get("DatasetName") or ""
                    user = (ev.get("UserId") or "").lower()
                    rows.append(
                        {
                            "ts": _dt.datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            ).replace(tzinfo=None),
                            # The audit log's own record id makes re-reading an
                            # overlapping day idempotent.
                            "event_key": str(ev.get("Id") or f"{ts}|{user}|{item}")[:80],
                            "user": user,
                            "action": ev.get("Activity") or "",
                            "item": item,
                            "workspace": ev.get("WorkspaceName")
                            or ws_by_item.get(item.lower(), ""),
                        }
                    )
                cont = data.get("continuationToken")
                if not cont:
                    break
        except ScanError:
            failed_days.append(day.isoformat())
            consecutive += 1
            if consecutive >= 3:
                # Consistent refusal means we walked past what is retained.
                break
            day = day - _dt.timedelta(days=1)
            continue
        consecutive = 0
        day = day - _dt.timedelta(days=1)

    return {"rows": rows, "failed_days": failed_days}


def fetch_activity(
    creds: Credentials,
    days=30,
    max_view_events=100_000,
    ws_by_item=None,
    activity_filter=None,
):
    """
    Pull activity events via GetActivityEvents (one UTC day per call, ~30 days
    retained) and return aggregates plus a bounded, most-recent sample.
    """
    token = get_token(creds)
    ws_by_item = {k.lower(): v for k, v in (ws_by_item or {}).items()}
    today = _dt.datetime.now(_dt.UTC).date()

    from collections import Counter

    # EVERY report view is kept, not a slice of the raw firehose. The old
    # bounded sample filled up from the newest day first — on this tenant that
    # was 1,000 events all from today, of which 10 were views, against 8,896 in
    # the window. The log showed 10 of 8,896.
    view_events: list[dict] = []
    total = views = 0
    view_reports: Counter = Counter()
    view_users: Counter = Counter()
    actions: Counter = Counter()
    daily: dict[str, dict] = {}
    daily_users: dict[str, set] = {}
    daily_viewers: dict[str, set] = {}
    active_users: set[str] = set()
    viewer_users: set[str] = set()
    touched_reports: set[str] = set()
    events_by_user: dict[str, int] = {}
    last_seen_user: dict[str, str] = {}
    events_by_ws: dict[str, int] = {}
    last_seen_ws: dict[str, str] = {}
    last_seen_item: dict[str, str] = {}
    last_view_item: dict[str, str] = {}
    last_refresh_item: dict[str, str] = {}

    def absorb(ev):
        nonlocal total, views
        action = ev.get("Activity", "") or ""
        ts = ev.get("CreationTime", "") or ""
        user = (ev.get("UserId", "") or "").lower()
        item = ev.get("ReportName", "") or ev.get("DatasetName", "") or ""
        ws = ev.get("WorkspaceName", "") or ws_by_item.get(item.lower(), "")

        total += 1
        actions[action or "Other"] += 1
        day = ts[:10]
        if day:
            b = daily.setdefault(
                day, {"date": day, "views": 0, "other": 0, "users": 0, "viewers": 0}
            )
            if action == VIEW_ACTION:
                b["views"] += 1
            else:
                b["other"] += 1

        if user:
            active_users.add(user)
            events_by_user[user] = events_by_user.get(user, 0) + 1
            if ts > last_seen_user.get(user, ""):
                last_seen_user[user] = ts
            if day:
                daily_users.setdefault(day, set()).add(user)

        if ws:
            k = ws.lower()
            events_by_ws[k] = events_by_ws.get(k, 0) + 1
            if ts > last_seen_ws.get(k, ""):
                last_seen_ws[k] = ts

        if item:
            k = item.lower()
            if ts > last_seen_item.get(k, ""):
                last_seen_item[k] = ts
            if "Refresh" in action and ts > last_refresh_item.get(k, ""):
                last_refresh_item[k] = ts

        if action == VIEW_ACTION:
            views += 1
            if item:
                view_reports[item] += 1
                touched_reports.add(item)
                if ts > last_view_item.get(item.lower(), ""):
                    last_view_item[item.lower()] = ts
            if user:
                view_users[user] += 1
                viewer_users.add(user)
                if day:
                    daily_viewers.setdefault(day, set()).add(user)

        if action == VIEW_ACTION and len(view_events) < max_view_events:
            view_events.append(
                {
                    "ts": ts,
                    "user": ev.get("UserId", "") or "",
                    "action": action,
                    "report": item,
                    "ws": ws,
                }
            )

    # GetActivityEvents only serves roughly the last 30 days, and asking for a
    # day past that boundary is a hard 400. Retention is not a scan failure —
    # stop at the oldest day the tenant will actually serve and report how far
    # back we got.
    days_covered = 0
    stopped_reason = ""
    failed_days: list[str] = []
    consecutive_failures = 0
    # Retention shows up as failures that never stop; a transient upstream
    # error is one day. Give up only once several days in a row refuse.
    MAX_CONSECUTIVE_FAILURES = 3
    for back in range(days):
        day = today - _dt.timedelta(days=back)
        start = f"{day.isoformat()}T00:00:00.000Z"
        end = f"{day.isoformat()}T23:59:59.999Z"
        cont = None
        try:
            while True:
                if cont:
                    params = {"continuationToken": f"'{cont}'"}
                else:
                    params = {"startDateTime": f"'{start}'", "endDateTime": f"'{end}'"}
                    if activity_filter:
                        params["$filter"] = activity_filter
                data = _get(token, "/admin/activityevents", params)
                for ev in data.get("activityEventEntities", []):
                    absorb(ev)
                cont = data.get("continuationToken")
                if not cont:
                    break
        except ScanError as e:
            # Skip this day and carry on: a single upstream blip should cost
            # one day, not the other twenty-nine.
            failed_days.append(day.isoformat())
            consecutive_failures += 1
            stopped_reason = str(e)[:200]
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Consistent refusal means we have walked past what the audit
                # log still retains.
                break
            continue
        consecutive_failures = 0
        days_covered += 1

    for d, users in daily_users.items():
        if d in daily:
            daily[d]["users"] = len(users)
    for d, users in daily_viewers.items():
        if d in daily:
            daily[d]["viewers"] = len(users)

    view_events.sort(key=lambda e: e["ts"] or "", reverse=True)

    return {
        "events": view_events,
        "summary": {
            "total": total,
            "views": views,
            "exports": actions.get("ExportReport", 0) + actions.get("ExportTile", 0),
            "distinctUsers": len(active_users),
            "viewerUsers": len(viewer_users),
            "days": days_covered or days,
            "daysRequested": days,
            "partial": days_covered < days,
            "stoppedReason": stopped_reason if days_covered < days else "",
            "failedDays": failed_days,
            "truncated": days_covered < days,
            "viewEventsKept": len(view_events),
            "viewEventsComplete": len(view_events) >= views,
        },
        "topReports": [{"report": r, "views": c} for r, c in view_reports.most_common(15)],
        "topUsers": [{"user": u, "views": c} for u, c in view_users.most_common(15)],
        "actionMix": [{"action": a, "count": c} for a, c in actions.most_common()],
        "activityTypes": sorted(actions),
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "activeUsers": sorted(active_users),
        "touchedReports": sorted(touched_reports),
        "eventsByUser": events_by_user,
        "lastSeenByUser": last_seen_user,
        "eventsByWorkspace": events_by_ws,
        "lastSeenByWorkspace": last_seen_ws,
        "lastSeenByItem": last_seen_item,
        "lastSeenByReport": last_view_item,
        "lastRefreshByDataset": last_refresh_item,
        "filter": activity_filter or "",
    }
