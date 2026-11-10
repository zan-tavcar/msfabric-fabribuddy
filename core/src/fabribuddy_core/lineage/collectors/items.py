"""
Items collector: the Fabric REST item inventory, and lakehouse table nodes.

Runs after the scanner and fills two gaps:

1. Item types the Scanner API does not enumerate on every tenant (notably
   Notebook, DataPipeline and Lakehouse on some tenants).
2. The lakehouse tables themselves, which the scanner never returns.

Table enumeration goes through the OneLake table API rather than the Lakehouse
"List Tables" endpoint: it handles schema-enabled lakehouses correctly and
reports a synthetic `dbo` schema for those without schemas, so one code path
covers both.
"""

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ...fabric import items as items_api
from ...fabric import onelake as onelake_api
from ...fabric.client import AccessDenied, FabricClient, FabricError
from .. import ids, layers, repository
from ..models import (
    KIND_EXTERNAL,
    KIND_LAKEHOUSE_TABLE,
    KIND_MIRRORED_TABLE,
    KIND_WAREHOUSE_TABLE,
    SOURCE_DECLARED,
)

log = logging.getLogger("lineage.collect.items")


def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def collect(
    session: Session,
    client: FabricClient,
    *,
    customer_id: str,
    tenant_id: str,
    contributor_workspaces: set[str] | None = None,
) -> dict:
    """Enumerate items and lakehouse tables. Per-workspace failures are recorded, not raised."""
    # The scope (Contributor, narrowed by the Admin selection). None = no
    # narrowing, for direct callers; the service always passes it.
    scope = (
        None
        if contributor_workspaces is None
        else {w.lower() for w in contributor_workspaces}
    )
    contributor_workspaces = scope or set()
    errors: list[dict] = []
    counts = {"workspaces": 0, "items": 0, "lakehouses": 0, "tables": 0, "columns": 0}

    try:
        # Only workspaces where the SPN is at least Contributor: a Viewer role
        # lists items but can read neither definitions nor the Delta log.
        workspaces = items_api.list_workspaces(client, roles=items_api.CONTRIBUTOR_ROLES)
    except AccessDenied as e:
        # Without the Fabric API the base inventory still stands from the
        # scanner, so this is a degraded run rather than a failed one.
        return {
            "stats": counts,
            "errors": [
                {
                    "scope": "tenant",
                    "scope_name": "Fabric REST API",
                    "reason": "needs_contributor",
                    "message": str(e)[:500],
                }
            ],
        }

    all_lakehouses: list[tuple[str, str, str, str]] = []
    for ws in workspaces:
        ws_guid = ids.norm_id(ws.get("id"))
        if not ws_guid:
            continue
        # Fabric answers with every workspace the SPN is Contributor on; only
        # the selected ones may exist in the database.
        if scope is not None and ws_guid not in scope:
            continue
        ws_id = ids.workspace_id(ws_guid)
        ws_name = ws.get("displayName") or ws.get("name") or "(unnamed)"
        counts["workspaces"] += 1

        repository.upsert_workspace(
            session,
            customer_id,
            tenant_id,
            id=ws_id,
            workspace_guid=ws_guid,
            name=ws_name,
            ws_type=ws.get("type") or "",
            state=ws.get("state") or "",
            capacity_id=ids.norm_id(ws.get("capacityId")),
            has_contributor=ws_guid in contributor_workspaces,
        )

        try:
            entries = items_api.list_items(client, ws_guid)
        except (AccessDenied, FabricError) as e:
            errors.append(
                {
                    "scope": "workspace",
                    "scope_id": ws_id,
                    "scope_name": ws_name,
                    "reason": "needs_contributor"
                    if isinstance(e, AccessDenied)
                    else "error",
                    "message": str(e)[:500],
                }
            )
            continue

        lakehouses: list[tuple[str, str]] = []
        stores: list[tuple[str, str, str]] = []
        for entry in entries:
            item_guid = ids.norm_id(entry.get("id"))
            item_type = entry.get("type") or ""
            if not item_guid or item_type not in items_api.RELEVANT_TYPES:
                continue
            item_id = ids.item_id(ws_guid, item_guid)
            repository.upsert_item(
                session,
                customer_id,
                tenant_id,
                id=item_id,
                workspace_id=ws_id,
                item_guid=item_guid,
                item_type=item_type,
                display_name=entry.get("displayName") or "(unnamed)",
                description=entry.get("description") or "",
                modified_at=_parse_ts(entry.get("lastUpdatedDate")),
            )
            counts["items"] += 1
            if item_type == "Lakehouse":
                lakehouses.append((item_guid, entry.get("displayName") or "(unnamed)"))
            elif item_type in items_api.TABLE_STORE_TYPES:
                stores.append(
                    (item_guid, entry.get("displayName") or "(unnamed)", item_type)
                )

        for lh_guid, lh_name in lakehouses:
            counts["lakehouses"] += 1
            made, failed = _lakehouse_tables(
                session,
                client,
                customer_id,
                tenant_id,
                ws_guid=ws_guid,
                ws_id=ws_id,
                ws_name=ws_name,
                lh_guid=lh_guid,
                lh_name=lh_name,
            )
            counts["tables"] += made[0]
            counts["columns"] += made[1]
            errors.extend(failed)

        for store_guid, store_name, store_type in stores:
            made, failed = _store_tables(
                session,
                client,
                customer_id,
                tenant_id,
                ws_guid=ws_guid,
                ws_id=ws_id,
                ws_name=ws_name,
                item_guid=store_guid,
                item_name=store_name,
                item_type=store_type,
            )
            counts["tables"] += made
            errors.extend(failed)
        session.commit()

        all_lakehouses.extend((ws_guid, ws_name, g, n) for g, n in lakehouses)
        session.commit()

    # Shortcuts last, across all workspaces: a shortcut in one workspace often
    # targets a table in another, which must already be enumerated to link to.
    for ws_guid, ws_name, lh_guid, lh_name in all_lakehouses:
        made, failed = _shortcut_edges(
            session,
            client,
            customer_id,
            tenant_id,
            ws_guid=ws_guid,
            ws_name=ws_name,
            lh_guid=lh_guid,
            lh_name=lh_name,
        )
        counts["shortcuts"] = counts.get("shortcuts", 0) + made
        errors.extend(failed)

    session.commit()
    return {"stats": counts, "errors": errors}


def _lakehouse_tables(
    session: Session,
    client: FabricClient,
    customer_id: str,
    tenant_id: str,
    *,
    ws_guid: str,
    ws_id: str,
    ws_name: str,
    lh_guid: str,
    lh_name: str,
) -> tuple[tuple[int, int], list[dict]]:
    """Enumerate one lakehouse's tables and columns via the OneLake table API."""
    item_id = ids.item_id(ws_guid, lh_guid)
    errors: list[dict] = []
    seen: set[str] = set()
    n_tables = n_columns = 0

    try:
        schemas = onelake_api.list_schemas(client, ws_guid, lh_guid)
    except (AccessDenied, FabricError) as e:
        return (0, 0), [
            {
                "scope": "lakehouse",
                "scope_id": item_id,
                "scope_name": f"{ws_name} / {lh_name}",
                "reason": "needs_contributor" if isinstance(e, AccessDenied) else "error",
                "message": str(e)[:500],
            }
        ]

    for schema_name in schemas:
        try:
            tables = onelake_api.list_tables(client, ws_guid, lh_guid, schema_name)
        except (AccessDenied, FabricError) as e:
            errors.append(
                {
                    "scope": "schema",
                    "scope_id": f"{item_id}:{schema_name}",
                    "scope_name": f"{ws_name} / {lh_name} / {schema_name}",
                    "reason": "error",
                    "message": str(e)[:500],
                }
            )
            continue

        for table in tables:
            table_name = table.get("name")
            if not table_name:
                continue
            node_id = ids.lakehouse_table_id(ws_guid, lh_guid, schema_name, table_name)
            seen.add(node_id)
            layer, declared = layers.resolve(
                KIND_LAKEHOUSE_TABLE, lh_name, schema_name, table_name
            )
            repository.upsert_node(
                session,
                customer_id,
                tenant_id,
                id=node_id,
                kind=KIND_LAKEHOUSE_TABLE,
                layer=layer,
                layer_is_declared=declared,
                workspace_id=ws_id,
                item_id=item_id,
                schema_name=schema_name,
                table_name=table_name,
                display_path=f"{ws_name} / {lh_name} / {schema_name}.{table_name}",
            )
            n_tables += 1

            for ordinal, column in enumerate(table.get("columns") or []):
                name = column.get("name")
                if not name:
                    continue
                repository.upsert_column(
                    session,
                    customer_id,
                    tenant_id,
                    id=ids.column_id(node_id, name),
                    node_id=node_id,
                    name=name,
                    ordinal=column.get("position", ordinal),
                    data_type=column.get("type_text") or column.get("type_name") or "",
                    nullable=bool(column.get("nullable", True)),
                    description=column.get("comment") or "",
                )
                n_columns += 1

    # Only prune when we actually enumerated the lakehouse — a partial read
    # must never be mistaken for "these tables are gone".
    if seen and not errors:
        repository.mark_missing_nodes(
            session, customer_id, tenant_id, item_id=item_id, seen_ids=seen
        )
    return (n_tables, n_columns), errors


# --- warehouses, mirrored databases, SQL databases --------------------------
_STORE_KINDS = {
    "Warehouse": (KIND_WAREHOUSE_TABLE, ids.warehouse_table_id),
    # A Fabric SQL database's tables are relational storage like a
    # warehouse's; they share its lane.
    "SQLDatabase": (KIND_WAREHOUSE_TABLE, ids.warehouse_table_id),
    "MirroredDatabase": (KIND_MIRRORED_TABLE, ids.mirrored_table_id),
}


def _store_tables(
    session: Session,
    client: FabricClient,
    customer_id: str,
    tenant_id: str,
    *,
    ws_guid: str,
    ws_id: str,
    ws_name: str,
    item_guid: str,
    item_name: str,
    item_type: str,
) -> tuple[int, list[dict]]:
    """
    Tables of a warehouse / mirrored database / SQL database, from OneLake.

    These items publish every table as Delta under `Tables/<schema>/<table>`
    (a mirrored database without a default schema may put them directly under
    `Tables/`). Listing the folders over the DFS endpoint needs only the
    storage token the Delta collector already uses; columns then come from each
    table's Delta log in the Delta pass.
    """
    kind, make_id = _STORE_KINDS[item_type]
    item_id = ids.item_id(ws_guid, item_guid)
    root = f"{item_guid}/Tables"

    def folders(path: str) -> list[str]:
        return [
            p["name"].rsplit("/", 1)[-1]
            for p in onelake_api.list_paths(client, ws_guid, path)
            if str(p.get("isDirectory", "")).lower() == "true" and p.get("name")
        ]

    found: list[tuple[str, str]] = []
    try:
        for first in folders(root):
            if first.startswith("_"):
                continue
            children = folders(f"{root}/{first}")
            if "_delta_log" in children:
                found.append(("dbo", first))  # a table directly under Tables/
            else:
                found.extend(
                    (first, child) for child in children if not child.startswith("_")
                )
    except (AccessDenied, FabricError) as e:
        return 0, [
            {
                "scope": "item",
                "scope_id": item_id,
                "scope_name": f"{ws_name} / {item_name}",
                "reason": "needs_contributor" if isinstance(e, AccessDenied) else "error",
                "message": str(e)[:500],
            }
        ]

    seen: set[str] = set()
    for schema, table in found:
        node_id = make_id(ws_guid, item_guid, schema, table)
        seen.add(node_id)
        layer, declared = layers.resolve(kind)
        repository.upsert_node(
            session,
            customer_id,
            tenant_id,
            id=node_id,
            kind=kind,
            layer=layer,
            layer_is_declared=declared,
            workspace_id=ws_id,
            item_id=item_id,
            schema_name=schema,
            table_name=table,
            display_path=f"{ws_name} / {item_name} / {schema}.{table}",
        )
    if seen:
        repository.mark_missing_nodes(
            session, customer_id, tenant_id, item_id=item_id, seen_ids=seen
        )
    return len(found), []


# --- shortcuts --------------------------------------------------------------
def _shortcut_edges(
    session: Session,
    client: FabricClient,
    customer_id: str,
    tenant_id: str,
    *,
    ws_guid: str,
    ws_name: str,
    lh_guid: str,
    lh_name: str,
) -> tuple[int, list[dict]]:
    """
    A shortcut under `Tables/` is a table whose data lives elsewhere.

    Recorded as a *declared* edge from the target to the shortcut table: the
    definition says so outright, which ranks above anything parsed. A OneLake
    target resolves to the real table when it is in scope; anything else — an
    out-of-scope workspace, ADLS, S3, Dataverse — becomes an external node
    named after the location, so the table never looks like it has no source.

    A schema shortcut (`Tables/<schema>` in a schema-enabled lakehouse) is a
    whole folder of shortcut tables: each table enumerated under that schema
    gets its own edge, from the same-named table in the target schema.
    """
    item_id = ids.item_id(ws_guid, lh_guid)
    try:
        shortcuts = items_api.list_shortcuts(client, ws_guid, lh_guid)
    except (AccessDenied, FabricError) as e:
        return 0, [
            {
                "scope": "lakehouse",
                "scope_id": item_id,
                "scope_name": f"{ws_name} / {lh_name}",
                "reason": "needs_contributor" if isinstance(e, AccessDenied) else "error",
                "message": f"Shortcuts unavailable: {str(e)[:400]}",
            }
        ]

    made = 0
    schema_tables: dict[str, list[str]] | None = None  # lazily, schema -> tables
    for shortcut in shortcuts:
        path = str(shortcut.get("path") or "").strip("/")
        name = str(shortcut.get("name") or "")
        if not name or not path.startswith("Tables"):
            continue  # Files/ shortcuts are folders, not tables
        segments = [p for p in path.split("/") if p]
        schema = segments[1] if len(segments) > 1 else "dbo"
        table_id = ids.lakehouse_table_id(ws_guid, lh_guid, schema, name)

        if repository.get_node(session, customer_id, tenant_id, table_id) is not None:
            links = [(table_id, shortcut, f"shortcut {path}/{name}")]
        elif len(segments) == 1:
            # `Tables/<name>` that is not a table: in a schema-enabled lakehouse
            # this is a schema shortcut — every table under the schema is a
            # shortcut table, each sourced from the same-named table in the
            # target schema.
            if schema_tables is None:
                schema_tables = _tables_by_schema(
                    session, customer_id, tenant_id, ids.item_id(ws_guid, lh_guid)
                )
            tables = schema_tables.get(name.lower(), [])
            links = [
                (
                    ids.lakehouse_table_id(ws_guid, lh_guid, real_schema, table),
                    _schema_shortcut_for_table(shortcut, table),
                    f"schema shortcut {path}/{name} ({table})",
                )
                for real_schema, table in tables
            ]
        else:
            continue  # not enumerated as a table (broken shortcut) — nothing to link

        for to_id, resolved, via_ref in links:
            source_id = _shortcut_source(session, customer_id, tenant_id, resolved)
            if source_id is None:
                continue
            repository.upsert_edge(
                session,
                customer_id,
                tenant_id,
                from_node_id=source_id,
                to_node_id=to_id,
                via_item_id=item_id,
                via_ref=via_ref,
                write_mode="shortcut",
                source=SOURCE_DECLARED,
                confidence="high",
                is_dynamic=False,
            )
            made += 1
    return made, []


def _tables_by_schema(
    session, customer_id: str, tenant_id: str, item_id: str
) -> dict[str, list[tuple[str, str]]]:
    """A lakehouse's enumerated tables, keyed by lower-cased schema name."""
    out: dict[str, list[tuple[str, str]]] = {}
    for node in repository.list_nodes(
        session, customer_id, tenant_id, kind=KIND_LAKEHOUSE_TABLE, item_id=item_id
    ):
        if node.schema_name and node.table_name:
            out.setdefault(node.schema_name.lower(), []).append(
                (node.schema_name, node.table_name)
            )
    return out


def _schema_shortcut_for_table(shortcut: dict, table: str) -> dict:
    """
    The table-level shortcut one table of a schema shortcut amounts to.

    A schema shortcut targets a folder of tables — `Tables/<schema>` in a
    schema-enabled lakehouse, `Tables` in one without schemas, or a folder of
    Delta tables elsewhere — so table `t` in it is `<target>/t`.
    """
    target = dict(shortcut.get("target") or {})
    kind = str(target.get("type") or "")
    if kind == "OneLake":
        one = dict(target.get("oneLake") or {})
        base = str(one.get("path") or "Tables").strip("/")
        one["path"] = f"{base}/{table}"
        target["oneLake"] = one
    else:
        key = kind[:1].lower() + kind[1:]
        props = dict(target.get(key) or {})
        for field in ("subpath", "deltaLakeFolder"):
            if props.get(field):
                props[field] = f"{str(props[field]).rstrip('/')}/{table}"
                break
        else:
            props.setdefault("tableName", table)
        target[key] = props
    return {**shortcut, "name": table, "target": target}


def _shortcut_source(session, customer_id, tenant_id, shortcut: dict) -> str | None:
    target = shortcut.get("target") or {}
    kind = str(target.get("type") or "")
    if kind == "OneLake":
        one = target.get("oneLake") or {}
        ws, item, tpath = (
            ids.norm_id(one.get("workspaceId")),
            ids.norm_id(one.get("itemId")),
            str(one.get("path") or "").strip("/"),
        )
        parts = [p for p in tpath.split("/") if p]
        if parts and parts[0] == "Tables" and len(parts) >= 2:
            schema, table = (parts[1], parts[2]) if len(parts) >= 3 else ("dbo", parts[1])
            for make_id in (
                ids.lakehouse_table_id,
                ids.warehouse_table_id,
                ids.mirrored_table_id,
            ):
                candidate = make_id(ws, item, schema, table)
                if repository.get_node(session, customer_id, tenant_id, candidate):
                    return candidate
        detail = {"kind": "OneLake", "workspace": ws, "item": item}
        obj = tpath or "(root)"
    else:
        key = kind[:1].lower() + kind[1:]
        props = target.get(key) or {}
        detail = {
            k: v
            for k, v in {
                "kind": kind or "external",
                "url": props.get("location") or props.get("environmentDomain") or "",
                "connection": props.get("connectionId") or "",
            }.items()
            if v
        }
        obj = str(
            props.get("tableName")
            or props.get("subpath")
            or props.get("deltaLakeFolder")
            or shortcut.get("name")
            or "(unknown)"
        ).strip("/")

    node_id = ids.external_id(detail, obj, tenant_id)
    if repository.get_node(session, customer_id, tenant_id, node_id) is None:
        layer, declared = layers.resolve(KIND_EXTERNAL)
        label = detail.get("url") or detail.get("kind") or "External"
        repository.upsert_node(
            session,
            customer_id,
            tenant_id,
            id=node_id,
            kind=KIND_EXTERNAL,
            layer=layer,
            layer_is_declared=declared,
            schema_name="",
            table_name=obj,
            display_path=f"External sources / {label} / {obj}",
            connection_hash=ids.connection_hash(detail),
            connection_detail=detail,
        )
        session.flush()  # the edge below carries a FK to this node
    return node_id
