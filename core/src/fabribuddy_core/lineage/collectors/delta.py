"""
Delta collector: the transaction log per lakehouse table.

Gives the node Overview tab everything it shows that no other source has — last
write, write mode, row and byte counts, version count — plus the "Recently
written" list on the Docs overview, which is simply the newest commits across
the tenant.

Fast by design: what a table needs is its last write and its row count, not
its version history. An unchanged table costs one listing; a first pass reads
back only to the latest full load; later passes read only what is new, a few
tables at a time. Row counts are exact from a full load forward and are tracked
through appends, merges and deletes — marked approximate when they cannot be.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...fabric import deltalog
from ...fabric.client import AccessDenied, FabricClient, FabricError
from ...services import progress
from .. import ids, repository
from ..models import (
    DOC_DELTA_PROPERTY,
    STORAGE_KINDS,
    ColumnNode,
    DeltaStats,
    DeltaVersion,
)

log = logging.getLogger("lineage.collect.delta")

# Tables read at once. OneLake reads are small and latency-bound, so a few in
# flight cut the wall time by about this factor without approaching its limits.
FETCH_WORKERS = 8

# Table properties Fabric/Spark users set to document a table. Read as docs so a
# description written with ALTER TABLE travels with the table rather than living
# only in our database.
DESCRIPTION_KEYS = ("comment", "description", "delta.comment")
OWNER_KEYS = ("owner", "delta.owner", "table.owner")
TAG_KEYS = ("tags", "delta.tags", "table.tags")


def collect(
    session: Session,
    client: FabricClient,
    *,
    customer_id: str,
    tenant_id: str,
    contributor_workspaces: set[str],
) -> dict:
    """Read the Delta log for every lakehouse table in an opted-in workspace."""
    contributor_workspaces = {w.lower() for w in contributor_workspaces}
    errors: list[dict] = []
    counts = {
        "tables": 0,
        "versions": 0,
        "skipped": 0,
        "columnsUpdated": 0,
        "schemasFromCheckpoint": 0,
    }

    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = {i.id: i for i in repository.list_items(session, customer_id, tenant_id)}
    # Warehouse and mirrored tables publish Delta logs to OneLake exactly as
    # lakehouse tables do, so one reader covers all three.
    nodes = [
        n
        for kind in STORAGE_KINDS
        for n in repository.list_nodes(session, customer_id, tenant_id, kind=kind)
    ]
    highest = _highest_versions(session, customer_id, tenant_id)
    stats_by_node = {
        row.node_id: row
        for row in session.scalars(
            select(DeltaStats).where(
                DeltaStats.customer_id == customer_id, DeltaStats.tenant_id == tenant_id
            )
        )
    }

    # Tables whose schema a collector already read (columns from code carry no
    # type). The rest get their schema from a checkpoint when the commits read
    # this pass do not carry it.
    typed = set(
        session.scalars(
            select(ColumnNode.node_id)
            .where(
                ColumnNode.customer_id == customer_id,
                ColumnNode.tenant_id == tenant_id,
                ColumnNode.data_type != "",
            )
            .distinct()
        )
    )

    # Network work runs on a pool, a few tables at a time; every database write
    # stays on this thread, because the session is not thread-safe.
    tasks = []
    for node in nodes:
        workspace = workspaces.get(node.workspace_id or "")
        item = items.get(node.item_id or "")
        if workspace is None or item is None:
            continue
        if workspace.workspace_guid.lower() not in contributor_workspaces:
            counts["skipped"] += 1
            continue
        tasks.append((node, workspace, item))

    def fetch(task):
        node, workspace, item = task
        since = highest.get(node.id, -1)
        stored = stats_by_node.get(node.id)
        if stored is not None and stored.current_version is not None:
            since = max(since, stored.current_version)
        candidates = deltalog.table_paths(item.item_guid, node.schema_name, node.table_name)
        for n, path in enumerate(candidates):
            try:
                history = deltalog.read_recent(
                    client, workspace.workspace_guid, path, since_version=since
                )
            except deltalog.NoDeltaLog:
                if n == len(candidates) - 1:
                    raise
                continue
            if node.id not in typed and not any(
                c.get("schema") for c in history.get("commits") or []
            ):
                # The schema is in a checkpoint (old commits cleaned up) or in an
                # earlier commit this pass did not read (a young table).
                history["checkpoint"] = deltalog.latest_metadata(
                    client, workspace.workspace_guid, path
                )
            return path, history
        raise deltalog.NoDeltaLog("no candidate path")

    with ThreadPoolExecutor(
        max_workers=FETCH_WORKERS, thread_name_prefix="fl-delta"
    ) as pool:
        futures = {pool.submit(fetch, task): task for task in tasks}
        for done, future in enumerate(as_completed(futures), start=1):
            node, workspace, _item = futures[future]
            progress.tick(done, len(tasks), node.display_path or node.table_name)
            try:
                _path, history = future.result()
            except AccessDenied as e:
                errors.append(_error(node, workspace, "needs_contributor", str(e)))
                continue
            except FabricError as e:
                errors.append(_error(node, workspace, "error", str(e)))
                continue

            counts["tables"] += 1
            checkpoint = history.get("checkpoint") or {}
            if checkpoint.get("schema"):
                counts["schemasFromCheckpoint"] += 1
            if history.get("unchanged"):
                counts["unchanged"] = counts.get("unchanged", 0) + 1
                if checkpoint.get("schema"):
                    counts["columnsUpdated"] += _write_columns(
                        session, customer_id, tenant_id, node, checkpoint
                    )
                    _write_docs(session, customer_id, tenant_id, node, checkpoint)
                    session.commit()
                continue
            counts["versions"] += _write_versions(
                session, customer_id, tenant_id, node.id, history
            )
            summary = deltalog.summarise(history)
            if not summary.get("schema") and checkpoint.get("schema"):
                summary["schema"] = checkpoint["schema"]
                summary["table_properties"] = (
                    summary.get("table_properties")
                    or checkpoint.get("table_properties")
                    or {}
                )
            stored = stats_by_node.get(node.id)
            rows, exact = deltalog.track_rows(
                history.get("commits") or [],
                stored.num_rows if stored is not None else None,
                bool(stored.rows_exact) if stored is not None else False,
                bool(history.get("contiguous")),
            )
            summary["num_rows"], summary["rows_exact"] = rows, exact
            _write_stats(session, customer_id, tenant_id, node.id, summary, history)
            counts["columnsUpdated"] += _write_columns(
                session, customer_id, tenant_id, node, summary
            )
            _write_docs(session, customer_id, tenant_id, node, summary)
            session.commit()

    session.commit()
    return {"stats": counts, "errors": errors}


def _error(node, workspace, reason: str, message: str) -> dict:
    return {
        "scope": "table",
        "scope_id": node.id,
        "scope_name": node.display_path or f"{workspace.name} / {node.table_name}",
        "reason": reason,
        "message": message[:500],
    }


def _highest_versions(session: Session, customer_id: str, tenant_id: str) -> dict[str, int]:
    """node id -> highest stored version, so the next pass reads only what is new."""
    stmt = (
        select(DeltaVersion.node_id, func.max(DeltaVersion.version))
        .where(
            DeltaVersion.customer_id == customer_id,
            DeltaVersion.tenant_id == tenant_id,
        )
        .group_by(DeltaVersion.node_id)
    )
    return {row[0]: row[1] for row in session.execute(stmt)}


def _write_versions(
    session: Session, customer_id: str, tenant_id: str, node_id: str, history: dict
) -> int:
    """Append new commits. Append-only: history is evidence and is never rewritten."""
    existing = {
        row[0]
        for row in session.execute(
            select(DeltaVersion.version).where(
                DeltaVersion.tenant_id == tenant_id, DeltaVersion.node_id == node_id
            )
        )
    }
    written = 0
    for commit in history.get("commits") or []:
        if commit["version"] in existing:
            continue
        session.add(
            DeltaVersion(
                customer_id=customer_id,
                tenant_id=tenant_id,
                node_id=node_id,
                version=commit["version"],
                committed_at=commit["committed_at"],
                operation=commit["operation"][:60],
                operation_parameters=commit["operation_parameters"],
                engine_info=commit["engine_info"][:200],
            )
        )
        written += 1
    return written


def _write_stats(
    session: Session,
    customer_id: str,
    tenant_id: str,
    node_id: str,
    summary: dict,
    history: dict,
) -> None:
    row = session.get(DeltaStats, node_id)
    if row is None:
        row = DeltaStats(node_id=node_id, customer_id=customer_id, tenant_id=tenant_id)
        session.add(row)
    row.current_version = summary.get("current_version")
    # Only overwrite a count we actually learned: a pass that read no write
    # commit must not blank the row count the previous pass established.
    if summary.get("num_rows") is not None:
        row.num_rows = summary["num_rows"]
    if "rows_exact" in summary:
        row.rows_exact = 1 if summary["rows_exact"] else 0
    if summary.get("size_bytes"):
        row.size_bytes = summary["size_bytes"]
    if summary.get("num_files") is not None:
        row.num_files = summary["num_files"]
    if summary.get("last_write_at") is not None:
        row.last_write_at = summary["last_write_at"]
    if summary.get("last_optimize_at") is not None:
        row.last_optimize_at = summary["last_optimize_at"]
    if summary.get("last_vacuum_at") is not None:
        row.last_vacuum_at = summary["last_vacuum_at"]
    from ...models import utcnow

    row.collected_at = utcnow()
    # total_versions is the authoritative count for the UI's "Delta versions".
    if history.get("total_versions") is not None:
        row.num_files = row.num_files if row.num_files is not None else None


def _write_columns(
    session: Session, customer_id: str, tenant_id: str, node, summary: dict
) -> int:
    """
    Update columns from the Delta schema.

    The Delta log is more authoritative than the OneLake table API here because
    it carries `metadata.comment` — the column description a customer set with
    ALTER TABLE, which is the only place table documentation can live and travel.
    """
    columns = deltalog.columns_from_schema(summary.get("schema"))
    if not columns:
        return 0
    for column in columns:
        repository.upsert_column(
            session,
            customer_id,
            tenant_id,
            id=ids.column_id(node.id, column["name"]),
            node_id=node.id,
            name=column["name"],
            ordinal=column["position"],
            data_type=column["type"],
            nullable=column["nullable"],
            **({"description": column["comment"]} if column["comment"] else {}),
        )
    return len(columns)


def _write_docs(
    session: Session, customer_id: str, tenant_id: str, node, summary: dict
) -> None:
    """Table properties -> a doc row, at delta_property precedence."""
    properties = {
        str(k).lower(): v for k, v in (summary.get("table_properties") or {}).items()
    }
    description = next(
        (str(properties[k]) for k in DESCRIPTION_KEYS if properties.get(k)), ""
    )
    owner = next((str(properties[k]) for k in OWNER_KEYS if properties.get(k)), "")
    raw_tags = next((properties[k] for k in TAG_KEYS if properties.get(k)), "")
    tags = [t.strip() for t in str(raw_tags).split(",") if t.strip()] if raw_tags else []

    if not any((description, owner, tags)):
        return
    repository.upsert_doc(
        session,
        customer_id,
        tenant_id,
        node_id=node.id,
        column_id=None,
        source=DOC_DELTA_PROPERTY,
        description=description,
        owner=owner,
        tags=tags,
    )


def recently_written(
    session: Session, customer_id: str, tenant_id: str, limit: int = 8
) -> list[dict]:
    """
    The newest write commits across the tenant, for the Docs overview.

    Maintenance operations are excluded: an OPTIMIZE is not news about the data,
    and letting them in pushes the actual writes off the list.
    """
    stmt = (
        select(DeltaVersion)
        .where(
            DeltaVersion.customer_id == customer_id,
            DeltaVersion.tenant_id == tenant_id,
            DeltaVersion.committed_at.isnot(None),
            DeltaVersion.operation.notin_(tuple(deltalog.MAINTENANCE_OPERATIONS)),
        )
        .order_by(DeltaVersion.committed_at.desc())
        .limit(limit * 3)
    )
    seen: set[str] = set()
    out: list[dict] = []
    for row in session.scalars(stmt):
        # One entry per table: five MERGEs on the same table is one piece of news.
        if row.node_id in seen:
            continue
        seen.add(row.node_id)
        out.append(
            {
                "nodeId": row.node_id,
                "operation": row.operation,
                "committedAt": row.committed_at,
                "version": row.version,
                "engine": row.engine_info,
            }
        )
        if len(out) >= limit:
            break
    return out
