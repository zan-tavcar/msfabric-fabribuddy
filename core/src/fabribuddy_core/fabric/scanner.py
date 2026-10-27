"""
Power BI Scanner API (`/admin/workspaces/getInfo`).

This is the base-tier collector: it needs only the "Service principals can use
read-only admin APIs" tenant setting and no workspace role, and it returns the
bulk of the inventory in one asynchronous round trip per 100 workspaces.

Documented ceilings we have to live inside:
  getInfo        500 requests/hour, 16 concurrent
  getScanResult  500 requests/hour
  getScanStatus  10,000 requests/hour

`datasetSchema` and `datasetExpressions` additionally require the tenant's
detailed-metadata settings to be enabled; when they are not, the call still
succeeds but the schema fields come back empty, so we ask for them and report
what we actually got rather than failing.
"""

import logging
import time

from .client import (
    API_POWERBI,
    SCOPE_POWERBI,
    AccessDenied,
    FabricClient,
    FabricError,
)

log = logging.getLogger("lineage.scanner")

BATCH_SIZE = 100
POLL_INTERVAL = 2
# Raised well above a permissions-only scan: asking for datasetSchema and
# datasetExpressions makes the payload far larger and the scan correspondingly
# slower on a model-heavy workspace.
POLL_TIMEOUT = 900

# Keys on a scanned workspace that are NOT item collections.
_NON_ITEM_KEYS = {
    "id",
    "name",
    "type",
    "state",
    "users",
    "capacityId",
    "isOnDedicatedCapacity",
    "defaultDatasetStorageFormat",
    "description",
    "hasWorkspaceLevelSettings",
}

# Scanner collection name -> the item type we store it as. Anything not listed
# is still collected, with its collection name title-cased, so a new Fabric
# item type appears in the inventory without a code change.
COLLECTION_TYPES = {
    "reports": "Report",
    "datasets": "SemanticModel",
    "dataflows": "Dataflow",
    "dashboards": "Dashboard",
    "datamarts": "Datamart",
    "lakehouses": "Lakehouse",
    "warehouses": "Warehouse",
    "notebooks": "Notebook",
    "dataPipelines": "DataPipeline",
    "sqlEndpoints": "SQLEndpoint",
    "kqlDatabases": "KQLDatabase",
    "eventstreams": "Eventstream",
    "mirroredDatabases": "MirroredDatabase",
}


def modified_workspace_ids(client: FabricClient, since: str | None = None) -> list[str]:
    """
    Workspace ids for the scan, using the `modified` endpoint.

    With `since`, this is the incremental path: only workspaces that changed
    are rescanned, which is the difference between a nightly job that costs 3
    getInfo calls and one that costs 60.
    """
    params = {"excludePersonalWorkspaces": "true"}
    if since:
        params["modifiedSince"] = since
    payload = client.request(
        "GET", f"{API_POWERBI}/admin/workspaces/modified", SCOPE_POWERBI, params=params
    )
    if payload.status_code != 200:
        raise FabricError(
            f"/admin/workspaces/modified returned {payload.status_code}: "
            f"{payload.text[:300]}"
        )
    return [w["id"] for w in payload.json() if w.get("id")]


def _start_scan(client: FabricClient, workspace_ids: list[str]) -> str:
    response = client.request(
        "POST",
        f"{API_POWERBI}/admin/workspaces/getInfo",
        SCOPE_POWERBI,
        params={
            "lineage": "true",
            "datasourceDetails": "true",
            "datasetSchema": "true",
            "datasetExpressions": "true",
            "getArtifactUsers": "true",
        },
        json_body={"workspaces": workspace_ids},
        allow_write=True,  # POST, but a read operation — see client._READ_ONLY_POSTS
    )
    if response.status_code not in (200, 202):
        raise FabricError(
            f"Scanner API refused the request ({response.status_code}). "
            f"{response.text[:200]}"
        )
    return response.json()["id"]


def _await_scan(client: FabricClient, scan_id: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        status = client.get_json(
            f"{API_POWERBI}/admin/workspaces/scanStatus/{scan_id}", SCOPE_POWERBI
        )
        state = status.get("status")
        if state == "Succeeded":
            return
        if state == "Failed":
            raise FabricError(f"Scanner API reported a failed scan ({scan_id}).")
        time.sleep(POLL_INTERVAL)
    raise FabricError(f"Scanner API did not finish within {POLL_TIMEOUT}s.")


def scan(
    client: FabricClient,
    workspace_ids: list[str],
    *,
    batch_size: int = BATCH_SIZE,
    progress=None,
) -> dict:
    """
    Run the scanner over every workspace and return the raw workspace payloads.

    A failing batch costs that batch, never the run: on a large tenant one bad
    hundred should not discard the other nine. Normalisation happens in the
    collector, not here, so the raw shape stays inspectable.
    """
    workspaces: list[dict] = []
    errors: list[dict] = []
    batches = [
        workspace_ids[i : i + batch_size] for i in range(0, len(workspace_ids), batch_size)
    ]

    for n, batch in enumerate(batches, start=1):
        log.info("scanner batch %d/%d (%d workspaces)", n, len(batches), len(batch))
        if progress:
            progress(n, len(batches))
        try:
            scan_id = _start_scan(client, batch)
            _await_scan(client, scan_id)
            result = client.get_json(
                f"{API_POWERBI}/admin/workspaces/scanResult/{scan_id}", SCOPE_POWERBI
            )
        except AccessDenied:
            # A permissions failure is the same for every batch, so stop rather
            # than burn the hourly quota rediscovering it 60 more times.
            raise
        except FabricError as e:
            errors.append(
                {
                    "scope": "workspace_batch",
                    "scope_name": f"batch {n}/{len(batches)}",
                    "reason": "error",
                    "message": str(e)[:500],
                }
            )
            continue
        workspaces.extend(result.get("workspaces") or [])

    # The detailed-metadata tenant settings are silently optional: the call
    # succeeds either way, and the only way to know is whether any model came
    # back with tables on it.
    schema_seen = any(
        ds.get("tables") for w in workspaces for ds in (w.get("datasets") or [])
    )

    return {
        "workspaces": workspaces,
        "errors": errors,
        "stats": {
            "workspacesScanned": len(workspaces),
            "batches": len(batches),
            "batchErrors": len(errors),
            "datasetSchemaAvailable": schema_seen,
        },
    }


def item_collections(workspace: dict):
    """
    Yield (item_type, item_dict) for everything in a scanned workspace.

    Generic on purpose: an unrecognised collection is still collected rather
    than dropped, because a Fabric item type we have never heard of is exactly
    the thing a governance tool should not be silently blind to.
    """
    for key, value in workspace.items():
        if key in _NON_ITEM_KEYS or not isinstance(value, list):
            continue
        item_type = COLLECTION_TYPES.get(key)
        if item_type is None:
            # "kqlDashboards" -> "KqlDashboard"
            singular = key[:-1] if key.endswith("s") else key
            item_type = singular[:1].upper() + singular[1:]
        for entry in value:
            if isinstance(entry, dict) and entry.get("id"):
                yield item_type, entry


def principal_entries(users: list[dict] | None) -> list[dict]:
    """
    Normalise Scanner user lists to the shape the access collector reads.

    The Scanner names the right after the collection it came from
    (`groupUserAccessRight`, `datasetUserAccessRight`, `reportUserAccessRight`,
    `artifactUserAccessRight`...), so the right is taken from whichever key ends
    in `AccessRight` rather than from a list that a new item type would break.
    """
    entries = []
    for user in users or []:
        right = next((v for k, v in user.items() if k.endswith("AccessRight") and v), "")
        principal_type = user.get("principalType") or "User"
        upn = user.get("emailAddress") or (
            user.get("identifier") if principal_type == "User" else ""
        )
        entries.append(
            {
                "right": right,
                "graphId": user.get("graphId") or "",
                "upn": upn or "",
                "name": user.get("displayName") or upn or user.get("identifier") or "",
                "type": principal_type,
            }
        )
    return entries
