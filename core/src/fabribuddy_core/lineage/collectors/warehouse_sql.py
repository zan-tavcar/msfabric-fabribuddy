"""
Warehouse SQL collector: the T-SQL that writes warehouse (and SQL-endpoint) tables.

For each warehouse and lakehouse SQL analytics endpoint in scope it stores, as
`code_artifact` rows on the item:

  modules/<schema>.<name>.sql     every view / procedure / function definition
  history/<hash>.sql              each distinct write statement run in the last
                                  30 days (query insights) — dbt models, CTAS
                                  scripts, COPY INTO loads

and creates a node per view, so a view's lineage has somewhere to land. The
parse pass then reads these with the T-SQL dialect like any other code.
History artifacts no longer seen are dropped: query insights keeps 30 days, and
a statement that stopped running should stop claiming to write its table.
"""

import hashlib
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...fabric import sql_endpoint
from ...fabric.client import AccessDenied, FabricClient, FabricError
from ...models import utcnow
from .. import ids, layers, repository
from ..models import KIND_WAREHOUSE_TABLE, CodeArtifact

log = logging.getLogger("lineage.collect.warehouse_sql")

SQL_ITEM_TYPES = {"Warehouse", "SQLEndpoint", "SQLAnalyticsEndpoint"}
MAX_HISTORY = 300


def collect(
    session: Session,
    client: FabricClient,
    *,
    customer_id: str,
    tenant_id: str,
    azure_tenant_id: str,
    contributor_workspaces: set[str],
    connect=sql_endpoint.connect,
) -> dict:
    scope = {w.lower() for w in contributor_workspaces}
    errors: list[dict] = []
    counts = {"endpoints": 0, "modules": 0, "statements": 0, "views": 0}

    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = repository.list_items(session, customer_id, tenant_id)
    storage_by_name = {
        (i.workspace_id, i.display_name.lower()): i
        for i in items
        if i.item_type in ("Lakehouse", "Warehouse")
    }

    for item in items:
        if item.item_type not in SQL_ITEM_TYPES:
            continue
        workspace = workspaces.get(item.workspace_id)
        if workspace is None or workspace.workspace_guid.lower() not in scope:
            continue
        where = f"{workspace.name} / {item.display_name}"
        try:
            host = (
                sql_endpoint.warehouse_host(
                    client, workspace.workspace_guid, item.item_guid
                )
                if item.item_type == "Warehouse"
                else sql_endpoint.workspace_host(azure_tenant_id, workspace.workspace_guid)
            )
            conn = connect(client, host, item.display_name)
        except sql_endpoint.SqlUnavailable as e:
            errors.append(_error(item, where, "error", str(e)))
            if "not installed" in str(e):
                break  # the same for every endpoint
            continue
        except (AccessDenied, FabricError) as e:
            reason = "needs_contributor" if isinstance(e, AccessDenied) else "error"
            errors.append(_error(item, where, reason, str(e)))
            continue

        try:
            modules = conn.run(sql_endpoint.Q_MODULES)
            try:
                history = conn.run(sql_endpoint.Q_HISTORY)
            except Exception as e:  # noqa: BLE001 — query insights can be off; modules still count
                history = []
                errors.append(
                    _error(item, where, "unsupported", f"Query insights unavailable: {e}")
                )
        except Exception as e:  # noqa: BLE001
            errors.append(_error(item, where, "error", f"Query failed: {str(e)[:300]}"))
            continue
        finally:
            conn.close()

        counts["endpoints"] += 1
        storage = (
            item
            if item.item_type == "Warehouse"
            else storage_by_name.get((item.workspace_id, item.display_name.lower()))
        )
        parts = store(session, customer_id, tenant_id, item, modules, history)
        counts["modules"] += parts["modules"]
        counts["statements"] += parts["statements"]
        if storage is not None:
            counts["views"] += _view_nodes(
                session, customer_id, tenant_id, workspace, storage, modules
            )
        session.commit()

    return {"stats": counts, "errors": errors}


def store(session, customer_id, tenant_id, item, modules, history) -> dict:
    """Upsert this endpoint's artifacts and drop the ones no longer present."""
    wanted: dict[str, str] = {}
    for schema, name, kind, definition in modules:
        if definition:
            wanted[f"modules/{schema}.{name}.sql"] = f"-- {kind}\n{definition.strip()}\n"
    seen_statements: set[str] = set()
    for command, started in history:
        text = (command or "").strip()
        normal = " ".join(text.split()).lower()
        digest = hashlib.sha1(normal.encode()).hexdigest()[:12]  # noqa: S324 — a key, not security
        if not text or digest in seen_statements:
            continue
        seen_statements.add(digest)
        wanted[f"history/{digest}.sql"] = f"-- last run {started}\n{text}\n"
        if len(seen_statements) >= MAX_HISTORY:
            break

    existing = {
        a.part_path: a
        for a in session.scalars(
            select(CodeArtifact).where(CodeArtifact.item_id == item.id)
        )
    }
    now = utcnow()
    for path, text in wanted.items():
        digest = hashlib.sha256(text.encode()).hexdigest()
        row = existing.get(path)
        if row is None:
            row = CodeArtifact(
                customer_id=customer_id,
                tenant_id=tenant_id,
                item_id=item.id,
                part_path=path,
            )
            session.add(row)
        if row.sha256 != digest:
            row.text, row.sha256 = text, digest
        row.language, row.fetch_mode, row.fetched_at = "sql", "sql", now
    stale = [p for p in existing if p not in wanted]
    if stale:
        session.execute(
            delete(CodeArtifact).where(
                CodeArtifact.item_id == item.id, CodeArtifact.part_path.in_(stale)
            )
        )
    return {
        "modules": sum(1 for p in wanted if p.startswith("modules/")),
        "statements": len(seen_statements),
    }


def _view_nodes(session, customer_id, tenant_id, workspace, storage, modules) -> int:
    made = 0
    for schema, name, kind, _definition in modules:
        if "VIEW" not in (kind or "").upper():
            continue
        node_id = ids.warehouse_table_id(
            workspace.workspace_guid, storage.item_guid, schema, name
        )
        layer, declared = layers.resolve(KIND_WAREHOUSE_TABLE)
        repository.upsert_node(
            session,
            customer_id,
            tenant_id,
            id=node_id,
            kind=KIND_WAREHOUSE_TABLE,
            layer=layer,
            layer_is_declared=declared,
            workspace_id=workspace.id,
            item_id=storage.id,
            schema_name=schema,
            table_name=name,
            display_path=f"{workspace.name} / {storage.display_name} / {schema}.{name} (view)",
            connection_detail={"objectType": "view"},
        )
        made += 1
    return made


def _error(item, where: str, reason: str, message: str) -> dict:
    return {
        "scope": "item",
        "scope_id": item.id,
        "scope_name": where,
        "reason": reason,
        "message": message[:500],
    }
