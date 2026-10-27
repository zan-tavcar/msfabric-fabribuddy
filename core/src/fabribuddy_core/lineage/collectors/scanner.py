"""
Scanner collector: raw Scanner API payloads -> workspaces, items, model tables.

This is the base tier — it needs no workspace role — so it establishes the
skeleton every later collector hangs things off. It deliberately does NOT
create lakehouse table nodes: the scanner does not enumerate them, and
inventing nodes from a semantic model's view of a lakehouse would produce
tables that do not exist.
"""

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ...fabric import scanner as scanner_api
from ...fabric.client import FabricClient
from .. import ids, layers, repository
from ..models import KIND_MODEL_TABLE, KIND_REPORT_VISUAL, SOURCE_DECLARED, DataNode

log = logging.getLogger("lineage.collect.scanner")


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
    workspace_ids: list[str],
    contributor_workspaces: set[str] | None = None,
) -> dict:
    """
    Run the scanner and write the inventory. Returns stats plus per-item errors.

    Idempotent: everything is an upsert on a deterministic id, so running it
    twice changes nothing but `last_seen_at`.
    """
    contributor_workspaces = {w.lower() for w in (contributor_workspaces or set())}
    result = scanner_api.scan(client, workspace_ids)
    errors = list(result["errors"])

    counts = {
        "workspaces": 0,
        "items": 0,
        "modelTables": 0,
        "modelColumns": 0,
        "reports": 0,
    }
    # model guid -> its workspace guid, across the whole scan: a report can use
    # a semantic model in another workspace.
    model_workspace = {
        ids.norm_id(entry.get("id")): ids.norm_id(ws.get("id"))
        for ws in result["workspaces"]
        for item_type, entry in scanner_api.item_collections(ws)
        if item_type == "SemanticModel"
    }
    reports: list[tuple[str, str, str, str, dict]] = []
    # Handed to the access collector instead of stored: the Scanner already
    # paid for these, and re-requesting them per item would spend the quota.
    workspace_users: dict[str, list[dict]] = {}
    item_users: dict[str, list[dict]] = {}

    for ws in result["workspaces"]:
        ws_guid = ids.norm_id(ws.get("id"))
        if not ws_guid:
            continue
        ws_id = ids.workspace_id(ws_guid)
        ws_name = ws.get("name") or "(unnamed)"

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
        counts["workspaces"] += 1
        if ws.get("users"):
            workspace_users[ws_id] = scanner_api.principal_entries(ws["users"])

        for item_type, entry in scanner_api.item_collections(ws):
            item_guid = ids.norm_id(entry.get("id"))
            if not item_guid:
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
                display_name=entry.get("name") or "(unnamed)",
                description=entry.get("description") or "",
                sensitivity_label=(entry.get("sensitivityLabel") or {}).get("labelId", ""),
                endorsement=(entry.get("endorsementDetails") or {}).get("endorsement", ""),
                created_by=entry.get("configuredBy") or entry.get("createdBy") or "",
                modified_at=_parse_ts(
                    entry.get("modifiedDateTime") or entry.get("lastUpdatedDate")
                ),
                storage_mode=entry.get("targetStorageMode") or "",
            )
            counts["items"] += 1
            if entry.get("users"):
                item_users[item_id] = scanner_api.principal_entries(entry["users"])

            if item_type == "SemanticModel":
                made = _semantic_model_tables(
                    session,
                    customer_id,
                    tenant_id,
                    ws_guid=ws_guid,
                    ws_id=ws_id,
                    ws_name=ws_name,
                    item_id=item_id,
                    model=entry,
                )
                counts["modelTables"] += made[0]
                counts["modelColumns"] += made[1]
            elif item_type == "Report":
                reports.append((ws_guid, ws_id, ws_name, item_id, entry))

    # After the loop, so every model node a report points at exists.
    for ws_guid, ws_id, ws_name, item_id, entry in reports:
        _report_node(
            session,
            customer_id,
            tenant_id,
            ws_guid=ws_guid,
            ws_id=ws_id,
            ws_name=ws_name,
            item_id=item_id,
            report=entry,
            model_workspace=model_workspace,
        )
        counts["reports"] += 1

    session.commit()
    stats = {**result["stats"], **counts}
    # No gap when model schemas are missing: with Contributor, the definitions
    # step reads each model's TMDL (tables, columns, measures, sources), and a
    # model it cannot read is reported there. The Scanner's schema is a fallback.
    return {
        "stats": stats,
        "errors": errors,
        "access": {"workspace_users": workspace_users, "item_users": item_users},
    }


def _report_node(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    ws_guid: str,
    ws_id: str,
    ws_name: str,
    item_id: str,
    report: dict,
    model_workspace: dict[str, str],
) -> None:
    """
    One node for every report, linked to the semantic model it is bound to.

    The Scanner names each report's model (`datasetId`), so a report gets its
    page and its place in the graph even when its definition cannot be read
    (no Contributor, an app copy, a legacy PBIX). The definitions step adds the
    visual-level detail on top for the reports it can read.
    """
    report_guid = ids.norm_id(report.get("id"))
    name = report.get("name") or "(unnamed)"
    node_id = ids.report_id(ws_guid, report_guid)
    repository.upsert_node(
        session,
        customer_id,
        tenant_id,
        id=node_id,
        kind=KIND_REPORT_VISUAL,
        layer=layers.layer_for_kind(KIND_REPORT_VISUAL),
        layer_is_declared=True,
        workspace_id=ws_id,
        item_id=item_id,
        schema_name="",
        table_name=name,
        display_path=f"{ws_name} / {name}",
    )
    model_guid = ids.norm_id(report.get("datasetId"))
    model_ws = model_workspace.get(model_guid) or ids.norm_id(
        report.get("datasetWorkspaceId")
    )
    if not model_guid or not model_ws:
        return  # a model outside the scan: the report stands alone
    model_node = ids.semantic_model_id(model_ws, model_guid)
    session.flush()
    if repository.get_node(session, customer_id, tenant_id, model_node) is None:
        return
    repository.upsert_edge(
        session,
        customer_id,
        tenant_id,
        from_node_id=model_node,
        to_node_id=node_id,
        via_item_id=item_id,
        via_ref="bound to semantic model",
        write_mode="report",
        source=SOURCE_DECLARED,
        confidence="high",
        is_dynamic=False,
    )


def _semantic_model_tables(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    ws_guid: str,
    ws_id: str,
    ws_name: str,
    item_id: str,
    model: dict,
) -> tuple[int, int]:
    """
    One node for the semantic model; its tables are that node's rows.

    Model-level on purpose: downstream of the storage layer, the questions are
    "which tables does this model use" and "which reports use this model".
    A node per model table (and measure-level edges between them) answered
    neither better and turned the Semantic model lane into a thousand boxes.
    The rows keep the table list; the TMDL pass fills in where each comes from.
    """
    from sqlalchemy import delete

    model_guid = ids.norm_id(model.get("id"))
    model_name = model.get("name") or "(unnamed)"
    node_id = ids.semantic_model_id(ws_guid, model_guid)

    # Per-table nodes from earlier builds go, with their edges (cascade).
    session.execute(
        delete(DataNode).where(
            DataNode.customer_id == customer_id,
            DataNode.tenant_id == tenant_id,
            DataNode.item_id == item_id,
            DataNode.kind == KIND_MODEL_TABLE,
            DataNode.id != node_id,
        )
    )
    layer, declared = layers.resolve(KIND_MODEL_TABLE)
    repository.upsert_node(
        session,
        customer_id,
        tenant_id,
        id=node_id,
        kind=KIND_MODEL_TABLE,
        layer=layer,
        layer_is_declared=declared,
        workspace_id=ws_id,
        item_id=item_id,
        schema_name="",
        table_name=model_name,
        display_path=f"{ws_name} / {model_name}",
    )

    n_tables = 0
    for ordinal, table in enumerate(model.get("tables") or []):
        table_name = table.get("name")
        if not table_name:
            continue
        columns = table.get("columns") or []
        measures = table.get("measures") or []
        repository.upsert_column(
            session,
            customer_id,
            tenant_id,
            id=ids.column_id(node_id, table_name),
            node_id=node_id,
            name=table_name,
            ordinal=ordinal,
            data_type=(
                "measures"
                if measures and not columns
                else str(table.get("storageMode") or "table")
            ),
            description=table.get("description") or "",
            is_measure=bool(measures and not columns),
        )
        n_tables += 1
    return 1, n_tables
