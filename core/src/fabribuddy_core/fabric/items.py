"""
Fabric REST item inventory (`/v1/workspaces`, `/v1/workspaces/{id}/items`).

Overlaps the Scanner API deliberately. The scanner is authoritative for
governance metadata the Fabric API does not expose (sensitivity labels,
endorsement, per-item users), while this is authoritative for the item *types*
the scanner does not yet enumerate on every tenant, and it is what tells us
whether the SPN can actually reach a workspace at Contributor tier.

Everything here is a plain GET, so it stays inside the read-only contract.
"""

import logging

from .client import API_FABRIC, SCOPE_FABRIC, AccessDenied, FabricClient, FabricError

log = logging.getLogger("lineage.items")

# Item types this product has anything to say about. Others are skipped to
# keep the inventory about data lineage rather than every artifact in Fabric.
RELEVANT_TYPES = {
    "Lakehouse",
    "Notebook",
    "DataPipeline",
    "Dataflow",
    "SemanticModel",
    "Report",
    "SQLEndpoint",
    "Warehouse",
    "MirroredDatabase",
    "SQLDatabase",
    "VariableLibrary",
    "CopyJob",
    "SparkJobDefinition",
    "Eventstream",
}

# Items whose tables live in OneLake as Delta under `Tables/<schema>/<table>`,
# besides lakehouses (which the OneLake table API enumerates).
TABLE_STORE_TYPES = {"Warehouse", "MirroredDatabase", "SQLDatabase"}


# Workspace roles at which the SPN can read item definitions and OneLake.
CONTRIBUTOR_ROLES = ("Admin", "Member", "Contributor")


def list_workspaces(
    client: FabricClient, roles: tuple[str, ...] | None = None
) -> list[dict]:
    """
    Workspaces the SPN holds a role in, optionally only those `roles`.

    The server-side `roles` filter is the one direct answer to "where am I at
    least Contributor" — it reports the caller's own role, so it needs no admin
    rights and no per-workspace probe.
    """
    params = {"roles": ",".join(roles)} if roles else None
    return client.paged(f"{API_FABRIC}/workspaces", SCOPE_FABRIC, params=params)


def list_job_instances(
    client: FabricClient, workspace_guid: str, item_guid: str
) -> list[dict]:
    """Run history for one item (notebook, pipeline, dataflow...). Read-only."""
    return client.paged(
        f"{API_FABRIC}/workspaces/{workspace_guid}/items/{item_guid}/jobs/instances",
        SCOPE_FABRIC,
    )


def list_items(
    client: FabricClient, workspace_guid: str, item_type: str | None = None
) -> list[dict]:
    """
    Items in one workspace, optionally filtered server-side by type.

    The type filter is worth using: on a workspace with thousands of reports,
    asking for just the Lakehouses is one small page instead of forty large ones.
    """
    params = {"type": item_type} if item_type else None
    return client.paged(
        f"{API_FABRIC}/workspaces/{workspace_guid}/items", SCOPE_FABRIC, params=params
    )


def probe_contributor(client: FabricClient, workspace_guid: str) -> bool:
    """
    Whether the SPN can read item *definitions* in this workspace.

    There is no API that answers "am I a Contributor" directly, and the
    workspace role assignment endpoint needs admin rights the base tier does
    not have. Listing items is the cheapest positive signal: the Fabric items
    API refuses outright without a workspace role, whereas the Scanner API
    works with none. A false positive here costs one failed getDefinition,
    which is recorded as a per-item error — so erring toward "try it" is right.
    """
    try:
        client.get_json(
            f"{API_FABRIC}/workspaces/{workspace_guid}", SCOPE_FABRIC, timeout=30
        )
    except AccessDenied:
        return False
    except FabricError:
        # Transient or unexpected: do not claim the workspace is off-limits on
        # the strength of one bad response.
        return False
    return True


def list_lakehouse_tables_via_workspace(
    client: FabricClient, workspace_guid: str, lakehouse_guid: str
) -> list[dict]:
    """
    Lakehouse "List Tables" — kept only as a fallback.

    The OneLake table API (app/fabric/onelake.py) is the primary path: it
    handles schema-enabled lakehouses correctly and reports a synthetic `dbo`
    schema for those without schemas, whereas this endpoint has known problems
    with schema-enabled lakehouses and with names containing spaces.
    """
    return client.paged(
        f"{API_FABRIC}/workspaces/{workspace_guid}/lakehouses/{lakehouse_guid}/tables",
        SCOPE_FABRIC,
    )


def list_shortcuts(client: FabricClient, workspace_guid: str, item_guid: str) -> list[dict]:
    """
    Every shortcut in an item, all subfolders included.

    `GET /v1/workspaces/{ws}/items/{id}/shortcuts` (OneLake Shortcuts - List
    Shortcuts). Each entry: `path` (where it lives, e.g. `Tables/dbo`), `name`,
    and `target` with `type` plus one of `oneLake` {workspaceId, itemId, path},
    `adlsGen2` / `amazonS3` / `googleCloudStorage` / `s3Compatible` /
    `azureBlobStorage` / `oneDriveSharePoint` {connectionId, location, subpath},
    or `dataverse` {environmentDomain, tableName, deltaLakeFolder}.
    """
    return client.paged(
        f"{API_FABRIC}/workspaces/{workspace_guid}/items/{item_guid}/shortcuts",
        SCOPE_FABRIC,
    )
