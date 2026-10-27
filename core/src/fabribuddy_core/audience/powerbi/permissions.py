"""
Per-item permission scan via the Power BI admin *scanner* API.

Why this API and not the per-item ones: `/admin/reports/{id}/users` would need
one request per report (thousands on a real tenant) against a 200/hour limit.
`workspaces/getInfo` returns up to 100 workspaces per call — workspace roles,
report grants and dataset grants together — under a 500/hour limit.

It is asynchronous: POST getInfo returns a scan id, you poll scanStatus until
Succeeded, then GET scanResult. Everything here degrades to partial results
rather than failing the whole scan, because on a large tenant one bad batch
should not cost you the other nine.
"""

import time

import requests

from .credentials import Credentials
from .scanner import API, ScanError, _get, get_token, norm_id

# The scanner API's own documented ceilings.
BATCH_SIZE = 100
POLL_INTERVAL = 2
POLL_TIMEOUT = 300

# Workspace roles that imply access to the workspace's content.
READ_ROLES = {"admin", "member", "contributor", "viewer"}

# Keys on a scanned workspace that are NOT Fabric items.
CLASSIC_KEYS = {
    "id",
    "name",
    "type",
    "state",
    "users",
    "reports",
    "dashboards",
    "datasets",
    "dataflows",
    "datamarts",
    "capacityId",
    "isOnDedicatedCapacity",
    "defaultDatasetStorageFormat",
    "description",
    "hasWorkspaceLevelSettings",
}


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_workspace_ids(creds: Credentials, include_personal=False, only_ids=None):
    """
    Workspace ids for the scan, paged (never truncated at 1000), narrowed to
    the Admin page's selection when `only_ids` (lower-cased GUIDs) is given.
    """
    from .scanner import _all_groups
    from .scanner import is_personal_workspace as scanner_is_personal

    token = get_token(creds)
    groups = _all_groups(token)
    out = []
    for g in groups:
        if not include_personal and scanner_is_personal(g):
            continue
        if g.get("state") and g.get("state") != "Active":
            continue
        if only_ids is not None and norm_id(g.get("id")) not in only_ids:
            continue
        if g.get("id"):
            out.append(g["id"])
    return out


def _start_scan(token, ids):
    r = requests.post(
        f"{API}/admin/workspaces/getInfo",
        params={
            "lineage": "true",
            "getArtifactUsers": "true",
            "datasourceDetails": "false",
            "datasetSchema": "false",
            "datasetExpressions": "false",
        },
        headers=_headers(token),
        json={"workspaces": ids},
        timeout=90,
    )
    if r.status_code == 429:
        wait = int(r.headers.get("Retry-After", "30"))
        time.sleep(wait)
        return _start_scan(token, ids)
    if r.status_code not in (200, 202):
        raise ScanError(
            f"Scanner API refused the request ({r.status_code}). Confirm the "
            f"SPN is allowed to use read-only admin APIs. {r.text[:200]}"
        )
    return r.json()["id"]


def _await_scan(token, scan_id):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        st = _get(token, f"/admin/workspaces/scanStatus/{scan_id}")
        state = st.get("status")
        if state == "Succeeded":
            return True
        if state == "Failed":
            raise ScanError(f"Scanner API reported a failed scan ({scan_id}).")
        time.sleep(POLL_INTERVAL)
    raise ScanError(f"Scanner API did not finish within {POLL_TIMEOUT}s.")


# How a person can get to a report, in Microsoft's own terms
# (learn.microsoft.com/power-bi/collaborate-share/service-share-dashboards):
#   - a workspace role (Admin, Member, Contributor, Viewer);
#   - direct access: Share -> Grant access, or a "Specific people" link. The
#     admin APIs record both as a grant to the person or group and do not
#     tell them apart;
#   - a "People in your organization" link: in the scan it is an entry with
#     principalType "None" ("whole organization level access"), and
#     /admin/widelySharedArtifacts/linksSharedToWholeOrganization lists them
#     with who created the link;
#   - Publish to web: public, listed by /admin/widelySharedArtifacts/publishedToWeb.
ORG_WIDE = "None"


def _widely_shared(token, kind: str) -> tuple[dict[str, dict], str]:
    """
    report id -> {right, sharer} for one widely-shared list (`linksSharedToWholeOrganization`
    or `publishedToWeb`), paged. Best effort: returns ({}, reason) when refused.
    """
    out: dict[str, dict] = {}
    params: dict | None = None
    try:
        for _ in range(200):  # 200/hour is the API's own ceiling
            data = _get(token, f"/admin/widelySharedArtifacts/{kind}", params)
            for entry in data.get("artifactAccessEntities") or []:
                if (entry.get("artifactType") or "Report") != "Report":
                    continue
                sharer = entry.get("sharer") or {}
                out[norm_id(entry.get("artifactId"))] = {
                    "right": entry.get("accessRight") or "Read",
                    "sharer": sharer.get("displayName") or sharer.get("emailAddress") or "",
                    "sharerUpn": (sharer.get("emailAddress") or "").lower(),
                }
            token_ = data.get("continuationToken")
            if not token_:
                break
            params = {"continuationToken": f"'{token_}'"}
    except ScanError as e:
        return out, str(e)[:200]
    return out, ""


def _principal(u, right_key):
    """Normalise one grant. graphId is what makes group expansion possible later."""
    name = u.get("displayName") or u.get("emailAddress") or u.get("identifier") or ""
    return {
        "name": name,
        "upn": (u.get("emailAddress") or u.get("identifier") or "").lower(),
        "graphId": u.get("graphId") or "",
        "type": u.get("principalType") or "User",
        "right": u.get(right_key) or "",
    }


def scan_permissions(
    creds: Credentials, workspace_ids, batch_size=BATCH_SIZE, progress=None
):
    """
    Returns workspace roles plus per-report and per-dataset grants.

    Never raises for a single bad batch — failures are collected and reported
    alongside whatever succeeded.
    """
    token = get_token(creds)
    workspaces, reports, datasets, fabric_items = [], [], [], []
    errors = []
    batches = [
        workspace_ids[i : i + batch_size] for i in range(0, len(workspace_ids), batch_size)
    ]

    for n, batch in enumerate(batches, start=1):
        if progress:
            progress(n, len(batches))
        try:
            scan_id = _start_scan(token, batch)
            _await_scan(token, scan_id)
            result = _get(token, f"/admin/workspaces/scanResult/{scan_id}")
        except ScanError as e:
            errors.append(f"batch {n}/{len(batches)}: {e}")
            continue

        for w in result.get("workspaces", []):
            ws_id, ws_name = norm_id(w.get("id")), w.get("name") or "(unnamed)"

            # Anything that is not a classic Power BI artifact is a Fabric
            # item (lakehouse, notebook, pipeline, eventhouse…). Collected
            # generically so a new Fabric item type shows up without a code
            # change rather than being silently dropped.
            for key, val in w.items():
                if key in CLASSIC_KEYS or not isinstance(val, list):
                    continue
                for it in val:
                    if isinstance(it, dict) and it.get("name"):
                        fabric_items.append(
                            {
                                "id": norm_id(it.get("id")),
                                "name": it.get("name"),
                                "kind": key,
                                "wsId": ws_id,
                                "wsName": ws_name,
                                "modifiedDateTime": it.get("lastUpdatedDate")
                                or it.get("modifiedDateTime")
                                or "",
                                "users": [
                                    _principal(u, "artifactUserAccessRight")
                                    for u in (it.get("users") or [])
                                ],
                            }
                        )
            workspaces.append(
                {
                    "id": ws_id,
                    "name": ws_name,
                    "type": w.get("type") or "",
                    "state": w.get("state") or "",
                    "users": [
                        _principal(u, "groupUserAccessRight")
                        for u in (w.get("users") or [])
                    ],
                }
            )

            for rep in w.get("reports") or []:
                grants = rep.get("users") or []
                # principalType "None" is organisation-wide access (a "People in
                # your organization" link), not a person: kept apart, never counted.
                org_wide = [u for u in grants if (u.get("principalType") or "") == ORG_WIDE]
                reports.append(
                    {
                        "id": norm_id(rep.get("id")),
                        "name": rep.get("name") or "(unnamed)",
                        "wsId": ws_id,
                        "wsName": ws_name,
                        "datasetId": norm_id(rep.get("datasetId")),
                        "createdBy": rep.get("createdBy") or "",
                        "modifiedDateTime": rep.get("modifiedDateTime") or "",
                        "reportType": rep.get("reportType") or "",
                        "endorsement": (rep.get("endorsementDetails") or {}).get(
                            "endorsement", ""
                        ),
                        "sensitivityLabel": (rep.get("sensitivityLabel") or {}).get(
                            "labelId", ""
                        ),
                        "users": [
                            _principal(u, "reportUserAccessRight")
                            for u in grants
                            if (u.get("principalType") or "") != ORG_WIDE
                        ],
                        "orgLink": {
                            "right": org_wide[0].get("reportUserAccessRight") or "Read",
                            "sharer": "",
                        }
                        if org_wide
                        else None,
                        "publishedToWeb": None,
                    }
                )

            for ds in w.get("datasets") or []:
                datasets.append(
                    {
                        "id": norm_id(ds.get("id")),
                        "name": ds.get("name") or "(unnamed)",
                        "wsId": ws_id,
                        "wsName": ws_name,
                        "configuredBy": ds.get("configuredBy") or "",
                        # Import vs DirectQuery vs Direct Lake, straight from the
                        # scanner API — not derivable from the plain admin scan.
                        "storageMode": ds.get("targetStorageMode") or "",
                        "contentProvider": ds.get("contentProviderType") or "",
                        "endorsement": (ds.get("endorsementDetails") or {}).get(
                            "endorsement", ""
                        ),
                        "createdDate": ds.get("createdDate") or "",
                        "users": [
                            _principal(u, "datasetUserAccessRight")
                            for u in (ds.get("users") or [])
                        ],
                    }
                )

    if not workspaces and errors:
        raise ScanError("; ".join(errors[:3]))

    # Organisation-wide links and Publish to web, from their own admin APIs:
    # they add who created the link, and catch links the scan did not mark.
    links, links_error = _widely_shared(token, "linksSharedToWholeOrganization")
    public, public_error = _widely_shared(token, "publishedToWeb")
    for rep in reports:
        link = links.get(rep["id"])
        if link:
            rep["orgLink"] = {"right": link["right"], "sharer": link["sharer"]}
        web = public.get(rep["id"])
        if web:
            rep["publishedToWeb"] = {"sharer": web["sharer"]}

    group_principals = {
        p["graphId"]: p["name"]
        for coll in (workspaces, reports, datasets, fabric_items)
        for item in coll
        for p in item["users"]
        if p["type"] == "Group" and p.get("graphId")
    }
    groups_seen = sorted(set(group_principals.values()))

    return {
        "workspaces": workspaces,
        "reports": reports,
        "datasets": datasets,
        "fabricItems": fabric_items,
        "groupPrincipals": group_principals,
        "stats": {
            "workspacesScanned": len(workspaces),
            "reports": len(reports),
            "datasets": len(datasets),
            "workspaceGrants": sum(len(w["users"]) for w in workspaces),
            "reportGrants": sum(len(r["users"]) for r in reports),
            "datasetGrants": sum(len(d["users"]) for d in datasets),
            "groupsSeen": groups_seen,
            "fabricItems": len(fabric_items),
            "batches": len(batches),
            "errors": errors,
            "orgLinks": sum(1 for r in reports if r.get("orgLink")),
            "publishedToWeb": sum(1 for r in reports if r.get("publishedToWeb")),
            # Empty when read; the reason otherwise (the UI says what is unknown).
            "widelySharedUnavailable": links_error or public_error,
        },
    }
