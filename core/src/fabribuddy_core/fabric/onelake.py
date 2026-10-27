"""
OneLake access: the table API for metadata, the filesystem API for the Delta log.

Two endpoints, one token audience (`https://storage.azure.com/.default` — the
table endpoint accepts the same audience as the filesystem endpoint):

  onelake.table.fabric.microsoft.com/delta  — Unity Catalog-compatible
      read-only metadata. List schemas, list tables, get table. This is the
      right way to enumerate a lakehouse: it reports a synthetic `dbo` schema
      for non-schema-enabled lakehouses, so schema-enabled and classic
      lakehouses share one code path.

  onelake.dfs.fabric.microsoft.com          — ADLS Gen2 filesystem. Needed for
      `_delta_log`, which is where history, operation parameters and the add
      stats (numRecords, size) live — none of which the table API exposes.

Reading `_delta_log` directly rather than through delta-rs is deliberate: it
avoids a heavy native dependency and the awkwardness of handing delta-rs a
bearer token, and we only need the commit metadata, never the data files.
"""

import json
import logging

from .client import (
    API_ONELAKE_DFS,
    API_ONELAKE_TABLE,
    SCOPE_STORAGE,
    FabricClient,
    FabricError,
)

log = logging.getLogger("lineage.onelake")

_UC = "api/2.1/unity-catalog"


def _base(workspace_guid: str, item_guid: str) -> str:
    return f"{API_ONELAKE_TABLE}/delta/{workspace_guid}/{item_guid}"


def list_schemas(client: FabricClient, workspace_guid: str, item_guid: str) -> list[str]:
    """
    Schema names in a lakehouse.

    A non-schema-enabled lakehouse yields exactly `["dbo"]`, which is what
    makes the caller's loop uniform.
    """
    payload = client.get_json(
        f"{_base(workspace_guid, item_guid)}/{_UC}/schemas",
        SCOPE_STORAGE,
        params={"catalog_name": item_guid},
    )
    names = [s.get("name") for s in (payload.get("schemas") or []) if s.get("name")]
    return names or ["dbo"]


def list_tables(
    client: FabricClient, workspace_guid: str, item_guid: str, schema_name: str
) -> list[dict]:
    """
    Tables in one schema, each with its columns.

    Unity Catalog's table object carries `columns` with name, type_text,
    position, nullable and comment — the comment being where a Delta column
    description surfaces, which is most of what the Columns tab shows.
    """
    payload = client.get_json(
        f"{_base(workspace_guid, item_guid)}/{_UC}/tables",
        SCOPE_STORAGE,
        params={"catalog_name": item_guid, "schema_name": schema_name},
    )
    return payload.get("tables") or []


def get_table(
    client: FabricClient, workspace_guid: str, item_guid: str, table_name: str
) -> dict:
    return client.get_json(
        f"{_base(workspace_guid, item_guid)}/{_UC}/tables/{table_name}",
        SCOPE_STORAGE,
    )


# --- filesystem / Delta log ------------------------------------------------
def list_paths(
    client: FabricClient, workspace_guid: str, path: str, *, recursive: bool = False
) -> list[dict]:
    """
    List a OneLake directory over the ADLS Gen2 filesystem API.

    Paged with `continuation`, which is a header here rather than a body field —
    unlike every other Microsoft API in this product.
    """
    out: list[dict] = []
    continuation = None
    while True:
        params = {
            "resource": "filesystem",
            "directory": path,
            "recursive": "true" if recursive else "false",
        }
        if continuation:
            params["continuation"] = continuation
        response = client.request(
            "GET", f"{API_ONELAKE_DFS}/{workspace_guid}", SCOPE_STORAGE, params=params
        )
        if response.status_code == 404:
            return out
        if response.status_code != 200:
            raise FabricError(
                f"OneLake list {path} returned {response.status_code}: "
                f"{response.text[:300]}"
            )
        out.extend(response.json().get("paths") or [])
        continuation = response.headers.get("x-ms-continuation")
        if not continuation:
            break
    return out


def read_file(client: FabricClient, workspace_guid: str, path: str) -> bytes:
    response = client.request(
        "GET", f"{API_ONELAKE_DFS}/{workspace_guid}/{path}", SCOPE_STORAGE
    )
    if response.status_code == 404:
        raise FabricError(f"OneLake path not found: {path}")
    if response.status_code != 200:
        raise FabricError(
            f"OneLake read {path} returned {response.status_code}: {response.text[:200]}"
        )
    return response.content


def read_delta_commit(
    client: FabricClient, workspace_guid: str, table_path: str, version: int
) -> list[dict]:
    """
    One `_delta_log/{version}.json` commit, as a list of actions.

    Delta commit files are newline-delimited JSON, one action per line, so this
    parses line by line rather than as a single document. A malformed line is
    skipped rather than failing the table: a partially-written commit at the
    head of the log is normal while a writer is mid-flight.
    """
    name = f"{version:020d}.json"
    raw = read_file(client, workspace_guid, f"{table_path}/_delta_log/{name}")
    actions: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            actions.append(json.loads(line))
        except json.JSONDecodeError:
            log.debug("skipping malformed action in %s v%s", table_path, version)
    return actions
