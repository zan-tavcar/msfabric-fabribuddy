"""
Building the API payloads.

Kept separate from the router so the shapes are testable without HTTP, and
separate from the repository so presentation decisions (what "Produced by" says,
how a lane is labelled) live in one place rather than being spread across
endpoints.

Everything here is a read. Nothing in this module triggers a collector.
"""

import logging
from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import repository as platform_repository
from ..models import ConnectedTenant
from . import graph as graph_module
from . import ids, repository, selectors
from .collectors import access as access_collector
from .collectors import delta as delta_collector
from .models import (
    DOC_DELTA_PROPERTY,
    DOC_TMDL,
    DOC_USER,
    KIND_EXTERNAL,
    KIND_LAKEHOUSE_TABLE,
    KIND_MODEL_TABLE,
    KIND_REPORT_VISUAL,
    LAYERS,
    STORAGE_ITEM_TYPES,
    STORAGE_KINDS,
    CodeArtifact,
    ColumnNode,
    DataNode,
    DeltaStats,
    DeltaVersion,
    Doc,
    Finding,
    Item,
    LineageEdge,
)

log = logging.getLogger("lineage.views")

LAYER_LABELS = {
    "source": "External source",
    "mirrored": "Mirrored database",
    "lakehouse": "Lakehouse table",
    "warehouse": "Warehouse table",
    "semantic": "Semantic model",
    "report": "Report",
}

KIND_LABELS = {
    KIND_LAKEHOUSE_TABLE: "Lakehouse table",
    "warehouse_table": "Warehouse table",
    "mirrored_table": "Mirrored table",
    KIND_EXTERNAL: "External source",
    KIND_MODEL_TABLE: "Semantic model",
    KIND_REPORT_VISUAL: "Report",
}

NO_DESCRIPTION = (
    "No description yet. Descriptions are read from Delta table properties, so "
    "one set on the table travels with it."
)
# Reports and semantic models carry no Delta properties: only what the team writes here.
NO_DESCRIPTION_POWER_BI = "No description yet."


class Context:
    """
    Everything a read needs, loaded once.

    A node page shows the node, its columns, its neighbours, its docs, its stats
    and its access. Loading those per-section is six round trips for one page.
    """

    def __init__(self, session: Session, customer_id: str, tenant_id: str):
        self.session = session
        self.customer_id = customer_id
        self.tenant_id = tenant_id
        self.nodes = {
            n.id: n for n in repository.list_nodes(session, customer_id, tenant_id)
        }
        self.items = {
            i.id: i for i in repository.list_items(session, customer_id, tenant_id)
        }
        self.workspaces = {
            w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
        }
        self._adjacency: graph_module.Adjacency | None = None
        self._column_adjacency: graph_module.ColumnAdjacency | None = None
        self._tenant: ConnectedTenant | None = None

    @property
    def tenant(self) -> ConnectedTenant | None:
        if self._tenant is None:
            self._tenant = self.session.get(ConnectedTenant, self.tenant_id)
        return self._tenant

    @property
    def adjacency(self) -> graph_module.Adjacency:
        if self._adjacency is None:
            self._adjacency = graph_module.load_adjacency(
                self.session, self.customer_id, self.tenant_id
            )
        return self._adjacency

    @property
    def column_adjacency(self) -> graph_module.ColumnAdjacency:
        if self._column_adjacency is None:
            self._column_adjacency = graph_module.load_column_adjacency(
                self.session, self.customer_id, self.tenant_id
            )
        return self._column_adjacency

    def label(self, node_id: str) -> str:
        """
        How a node is named in a list or on a graph card.

        A Delta table reads `schema.table`; everything else uses its own name,
        because `dbo.Net sales by country` is not how anyone refers to a visual.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return node_id
        if node.kind in STORAGE_KINDS:
            item = self.items.get(node.item_id or "")
            prefix = f"{item.display_name}." if item else ""
            return f"{prefix}{node.schema_name}.{node.table_name}".strip(".")
        return node.table_name or node.display_path

    def workspace_name(self, node_id: str) -> str:
        """The workspace a node sits in; empty for external sources."""
        node = self.nodes.get(node_id)
        workspace = self.workspaces.get(node.workspace_id or "") if node else None
        return workspace.name if workspace else ""

    def short(self, node_id: str) -> str:
        """The secondary line on a graph card."""
        node = self.nodes.get(node_id)
        if node is None:
            return ""
        item = self.items.get(node.item_id or "")
        if node.kind == KIND_EXTERNAL:
            detail = node.connection_detail or {}
            return str(
                detail.get("server")
                or detail.get("url")
                or detail.get("kind")
                or "External"
            )
        if node.kind in STORAGE_KINDS:
            return f"{item.display_name}.{node.schema_name}" if item else node.schema_name
        if item is not None:
            return item.storage_mode or item.item_type
        return ""


def node_brief(context: Context, node_id: str) -> dict:
    """The compact node shape used in lists, graphs and search results."""
    node = context.nodes.get(node_id)
    if node is None:
        return {"id": node_id, "name": node_id, "layer": "source", "kind": ""}
    return {
        "id": node.id,
        "name": context.label(node.id),
        "shortName": node.table_name or context.label(node.id),
        "sub": context.short(node.id),
        "layer": node.layer,
        "layerLabel": LAYER_LABELS.get(node.layer, node.layer.title()),
        "kind": node.kind,
        "kindLabel": KIND_LABELS.get(node.kind, node.kind),
        "path": node.display_path,
        "workspace": context.workspace_name(node.id),
    }


# --- overview --------------------------------------------------------------
def _touched_by(context: "Context", item_ids: set[str]) -> set[str]:
    """Every node an edge drawn through one of these items starts or ends at."""
    out: set[str] = set()
    for edge in context.adjacency.edges:
        if edge.via_item_id in item_ids:
            out.add(edge.from_node_id)
            out.add(edge.to_node_id)
    return out


def _ids(raw: str) -> set[str]:
    """`a,b,c` -> {a, b, c}; ids never contain a comma."""
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def scope_nodes(
    context: Context, *, workspace: str = "", item: str = "", table: str = "", q: str = ""
) -> set[str] | None:
    """
    The node ids a workspace / item / table filter selects, or None for no filter.

    The most specific filter wins: a table inside an item inside a workspace
    means that table. Ids are the ones the tree hands out (`ws:…`, `item:…`,
    node ids), so the UI never sends a name that could match twice.
    """
    # Each level may name several ids, comma-separated: any of them.
    tables, items, workspaces = _ids(table), _ids(item), _ids(workspace)
    scope: set[str] | None = None
    if tables:
        scope = {t for t in tables if t in context.nodes}
    elif items:
        # A storage item: its own tables. A notebook, pipeline or dataflow: the
        # tables its code writes and reads, since it holds none itself.
        scope = {n.id for n in context.nodes.values() if n.item_id in items}
        scope |= _touched_by(context, items)
    elif workspaces:
        # What lives in the workspace, plus what its code touches elsewhere: a
        # compute workspace of notebooks selects the tables they build.
        scope = {n.id for n in context.nodes.values() if n.workspace_id in workspaces}
        code = {i.id for i in context.items.values() if i.workspace_id in workspaces}
        scope |= _touched_by(context, code)
    if q.strip():
        # Search narrows whatever else is chosen: name, schema.table or path.
        needle = q.strip().lower()
        matched = {
            n.id
            for n in context.nodes.values()
            if needle in (n.table_name or "").lower()
            or needle in f"{n.schema_name}.{n.table_name}".lower()
            or needle in (n.display_path or "").lower()
        }
        scope = matched if scope is None else scope & matched
    return scope


def overview(
    context: Context, *, workspace: str = "", item: str = "", table: str = "", q: str = ""
) -> dict:
    """Metric cards, findings and the recently-written list, optionally scoped."""
    session, customer_id, tenant_id = (
        context.session,
        context.customer_id,
        context.tenant_id,
    )
    scope = scope_nodes(context, workspace=workspace, item=item, table=table, q=q)

    tables = [
        n
        for n in context.nodes.values()
        if n.kind in STORAGE_KINDS and (scope is None or n.id in scope)
    ]
    item_counts = repository.count_items_by_type(session, customer_id, tenant_id)

    columns = list(
        session.scalars(
            select(ColumnNode).where(
                ColumnNode.customer_id == customer_id,
                ColumnNode.tenant_id == tenant_id,
            )
        )
    )
    table_ids = {t.id for t in tables}
    table_columns = [c for c in columns if c.node_id in table_ids]
    documented = sum(1 for c in table_columns if (c.description or "").strip())
    pii_tables = {c.node_id for c in table_columns if c.is_pii}

    principals = set()
    for row in session.execute(
        select(
            access_collector.AccessGrant.principal_id, access_collector.AccessGrant.node_id
        ).where(
            access_collector.AccessGrant.customer_id == customer_id,
            access_collector.AccessGrant.tenant_id == tenant_id,
        )
    ):
        if scope is None or row[1] in scope:
            principals.add(row[0])

    findings = open_findings(context, scope=scope)
    recent = [r for r in _recent(context) if scope is None or r["nodeId"] in scope]
    workspaces = (
        len(context.workspaces)
        if scope is None
        else len(
            {context.nodes[n].workspace_id for n in scope if context.nodes[n].workspace_id}
        )
    )
    if scope is not None:
        # Producers of the scoped tables, not items inside the scope: a
        # lakehouse filter should count the notebooks that write it.
        producers = {
            e.via_item_id
            for n in scope
            for e in context.adjacency.edges
            if e.to_node_id == n and e.via_item_id
        }
        item_counts = {
            t: sum(
                1 for i in context.items.values() if i.id in producers and i.item_type == t
            )
            for t in ("Notebook", "DataPipeline", "Dataflow")
        }

    # Lakehouses and warehouses: every one in the tenant, or, filtered, the
    # ones that hold the tables in scope.
    stores = (
        {
            t: sum(1 for i in context.items.values() if i.item_type == t)
            for t in ("Lakehouse", "Warehouse")
        }
        if scope is None
        else {
            t: len(
                {
                    context.nodes[n].item_id
                    for n in scope
                    if context.nodes[n].item_id
                    and context.nodes[n].item_id in context.items
                    and context.items[context.nodes[n].item_id].item_type == t
                }
            )
            for t in ("Lakehouse", "Warehouse")
        }
    )

    return {
        "stats": [
            {"key": "Workspaces", "value": workspaces, "tone": "plain"},
            {"key": "Lakehouses", "value": stores["Lakehouse"], "tone": "plain"},
            {"key": "Warehouses", "value": stores["Warehouse"], "tone": "plain"},
            {"key": "Storage tables", "value": len(tables), "tone": "plain"},
            {
                "key": "Notebooks · pipelines · dataflows",
                "value": " · ".join(
                    str(item_counts.get(t, 0))
                    for t in ("Notebook", "DataPipeline", "Dataflow")
                ),
                "tone": "plain",
            },
            {
                "key": "Columns documented",
                "value": (
                    f"{round(documented / len(table_columns) * 100)}%"
                    if table_columns
                    else "—"
                ),
                "tone": "warn",
            },
            {"key": "Tables with PII", "value": len(pii_tables), "tone": "danger"},
            {
                "key": "Principals with data access",
                "value": len(principals),
                "tone": "plain",
            },
        ],
        "findings": findings,
        "recent": recent,
        "flow": flow(context, scope),
        "gaps": platform_repository.count_errors(session, customer_id, tenant_id),
    }


# Storage and sources are counted in tables; models and reports in items,
# because a model's tables and a report's visuals are not what people count.
_FLOW_COUNTS_ITEMS = {"semantic", "report"}


def flow(context: Context, scope: set[str] | None = None) -> dict:
    """
    The estate as lanes, source to report, and what each lane feeds.

    A lane counts its nodes (tables, sources) or, for models and reports, the
    items they belong to. A link from lane A to lane B counts the things in B
    fed by something in A, in B's unit: "12 warehouse tables are built from
    lakehouse tables", "3 models read lakehouse tables". Links inside a lane
    (bronze -> silver lakehouse tables) are counted the same way.
    """

    def in_scope(node_id: str) -> bool:
        return scope is None or node_id in scope

    def unit(node: DataNode) -> str:
        return (node.item_id or node.id) if node.layer in _FLOW_COUNTS_ITEMS else node.id

    lanes: dict[str, set[str]] = defaultdict(set)
    containers: dict[str, set[str]] = defaultdict(set)
    for node in context.nodes.values():
        if node.layer not in LAYERS or not in_scope(node.id):
            continue
        lanes[node.layer].add(unit(node))
        if node.layer in ("lakehouse", "warehouse", "mirrored") and node.item_id:
            containers[node.layer].add(node.item_id)

    links: dict[tuple[str, str], set[str]] = defaultdict(set)
    for edge in context.adjacency.edges:
        source = context.nodes.get(edge.from_node_id)
        target = context.nodes.get(edge.to_node_id)
        if (
            source is None
            or target is None
            or not in_scope(source.id)
            or not in_scope(target.id)
            or source.layer not in LAYERS
            or target.layer not in LAYERS
        ):
            continue
        links[(source.layer, target.layer)].add(unit(target))

    return {
        "lanes": [
            {
                "layer": layer,
                "label": LAYER_LABELS.get(layer, layer.title()),
                "count": len(lanes[layer]),
                "containers": len(containers[layer]),
            }
            for layer in LAYERS
        ],
        "links": [
            {"from": a, "to": b, "count": len(targets)}
            for (a, b), targets in sorted(
                links.items(),
                key=lambda kv: (LAYERS.index(kv[0][0]), LAYERS.index(kv[0][1])),
            )
        ],
    }


# What an item writing its own tables is called on its page.
_OWN_WRITER = {
    "Warehouse": "Its stored procedures",
    "MirroredDatabase": "Its mirroring",
    "Lakehouse": "Its shortcuts",
    "SQLDatabase": "Its own SQL",
}

# --- catalog --------------------------------------------------------------
# How a table gets written, by the kind of the item on its incoming edge.
_PRODUCER_KIND = {
    "Notebook": "Notebook",
    "SparkJobDefinition": "Spark job",
    "DataPipeline": "Pipeline",
    "CopyJob": "Copy job",
    "Dataflow": "Dataflow Gen2",
    "MirroredDatabase": "Mirroring",
    "Eventstream": "Eventstream",
    "Lakehouse": "Shortcut",
    "SemanticModel": "Semantic model",
    "Report": "Report",
}

# engineInfo of a Delta commit -> the kind of writer, for a table no parsed
# item claims. Evidence of *how* it was written, not *which item* wrote it.
_ENGINE_KIND = (
    ("mashup engine", "Dataflow Gen2"),
    ("datafactorycopy", "Pipeline"),
    ("apache-spark", "Spark"),
)
# Maintenance runs on Spark whatever wrote the data, so it says nothing about
# the writer.
_MAINTENANCE_OPS = ("OPTIMIZE", "VACUUM START", "VACUUM END", "SET TBLPROPERTIES")


def _table_name(node: DataNode) -> str:
    return f"{node.schema_name}.{node.table_name}" if node.schema_name else node.table_name


def _engine_kinds(context: "Context") -> dict[str, str]:
    """node id -> writer kind, from the most recent Delta commit that names an engine."""
    from sqlalchemy import func

    latest = (
        select(DeltaVersion.node_id, func.max(DeltaVersion.version).label("v"))
        .where(
            DeltaVersion.customer_id == context.customer_id,
            DeltaVersion.tenant_id == context.tenant_id,
            DeltaVersion.engine_info != "",
            DeltaVersion.operation.not_in(_MAINTENANCE_OPS),
        )
        .group_by(DeltaVersion.node_id)
        .subquery()
    )
    rows = context.session.execute(
        select(DeltaVersion.node_id, DeltaVersion.engine_info).join(
            latest,
            (DeltaVersion.node_id == latest.c.node_id)
            & (DeltaVersion.version == latest.c.v),
        )
    )
    out: dict[str, str] = {}
    for node_id, engine in rows:
        engine = (engine or "").lower()
        kind = next((k for marker, k in _ENGINE_KIND if marker in engine), None)
        if kind:
            out[node_id] = kind
    return out


def catalog(
    context: Context, *, workspace: str = "", item: str = "", table: str = "", q: str = ""
) -> dict:
    """
    One row per storage table, semantic model and report — the Docs catalog.

    Flat on purpose: the UI groups it workspace -> item -> table and derives the
    charts from the same rows, so what a bar counts is exactly what the table
    below it lists.
    """
    scope = scope_nodes(context, workspace=workspace, item=item, table=table, q=q)
    wanted_kinds = (*STORAGE_KINDS, KIND_MODEL_TABLE, KIND_REPORT_VISUAL)
    stats = {
        row.node_id: row
        for row in context.session.scalars(
            select(DeltaStats).where(
                DeltaStats.customer_id == context.customer_id,
                DeltaStats.tenant_id == context.tenant_id,
            )
        )
    }
    documented = {
        d.node_id
        for d in context.session.scalars(
            select(Doc).where(
                Doc.customer_id == context.customer_id,
                Doc.tenant_id == context.tenant_id,
                Doc.node_id.isnot(None),
            )
        )
        if (d.description or "").strip()
    }
    incoming: dict[str, list] = defaultdict(list)
    outgoing: dict[str, list] = defaultdict(list)
    for edge in context.adjacency.edges:
        incoming[edge.to_node_id].append(edge)
        outgoing[edge.from_node_id].append(edge)
    engine_kinds = _engine_kinds(context)
    # Per table: columns, documented columns, and whether any is personal data.
    # Counted in the database: a large estate has tens of thousands of columns.
    from sqlalchemy import case, func

    has_description = func.trim(func.coalesce(ColumnNode.description, "")) != ""
    column_counts: dict[str, list[int]] = {
        node_id: [total, documented_count or 0, pii or 0]
        for node_id, total, documented_count, pii in context.session.execute(
            select(
                ColumnNode.node_id,
                func.count(),
                func.sum(case((has_description, 1), else_=0)),
                func.sum(case((ColumnNode.is_pii, 1), else_=0)),
            )
            .where(
                ColumnNode.customer_id == context.customer_id,
                ColumnNode.tenant_id == context.tenant_id,
            )
            .group_by(ColumnNode.node_id)
        )
    }

    rows = []
    for node in context.nodes.values():
        if node.kind not in wanted_kinds or (scope is not None and node.id not in scope):
            continue
        item_row = context.items.get(node.item_id or "")
        workspace_row = context.workspaces.get(node.workspace_id or "")

        producers: dict[str, dict] = {}
        for edge in incoming[node.id]:
            via = context.items.get(edge.via_item_id or "")
            if via is None or via.id == node.item_id and via.item_type != "Lakehouse":
                continue
            kind = _PRODUCER_KIND.get(via.item_type, via.item_type)
            if edge.write_mode == "shortcut":
                kind = "Shortcut"
            producers.setdefault(
                via.id, {"item": via.display_name, "kind": kind, "mode": edge.write_mode}
            )

        models: set[str] = set()
        reports: set[str] = set()
        for edge in outgoing[node.id]:
            child = context.nodes.get(edge.to_node_id)
            if child is None:
                continue
            if child.kind == KIND_MODEL_TABLE:
                models.add(child.id)
                for further in outgoing[child.id]:
                    grandchild = context.nodes.get(further.to_node_id)
                    if grandchild is not None and grandchild.kind == KIND_REPORT_VISUAL:
                        reports.add(grandchild.id)
            elif child.kind == KIND_REPORT_VISUAL:
                reports.add(child.id)

        stat = stats.get(node.id)
        columns = column_counts.get(node.id, [0, 0, 0])
        # A model or report: what it is built on, by name.
        sources = (
            sorted(
                {
                    context.label(edge.from_node_id)
                    if context.nodes[edge.from_node_id].kind not in STORAGE_KINDS
                    else _table_name(context.nodes[edge.from_node_id])
                    for edge in incoming[node.id]
                    if edge.from_node_id in context.nodes
                },
                key=str.lower,
            )
            if node.kind in (KIND_MODEL_TABLE, KIND_REPORT_VISUAL)
            else []
        )
        rows.append(
            {
                "id": node.id,
                "name": node.table_name,
                "schema": node.schema_name or "",
                "kind": node.kind,
                "kindLabel": KIND_LABELS.get(node.kind, node.kind),
                "layer": node.layer,
                "workspaceId": node.workspace_id or "",
                "workspace": workspace_row.name if workspace_row else "",
                "itemId": node.item_id or "",
                "item": item_row.display_name if item_row else "",
                "itemType": item_row.item_type if item_row else "",
                "producers": list(producers.values()),
                "producerKind": _primary_producer(list(producers.values()), node)
                if producers
                else engine_kinds.get(node.id, "Unknown"),
                # "delta": the kind comes from the Delta log's engine, and no
                # item could be named as the writer.
                "producerEvidence": "code"
                if producers
                else ("delta" if node.id in engine_kinds else ""),
                "upstream": len(incoming[node.id]),
                "models": len(models),
                "reports": len(reports),
                "lastWriteAt": stat.last_write_at.isoformat()
                if stat and stat.last_write_at
                else None,
                "rows": stat.num_rows if stat else None,
                "sizeBytes": stat.size_bytes if stat else None,
                "documented": node.id in documented,
                "columns": columns[0],
                "columnsDocumented": columns[1],
                "pii": bool(columns[2]),
                "sources": sources,
                "storageMode": item_row.storage_mode if item_row else "",
            }
        )
    rows.sort(
        key=lambda r: (
            r["workspace"].lower(),
            r["item"].lower(),
            r["schema"],
            r["name"].lower(),
        )
    )
    return {"rows": rows}


def _primary_producer(producers: list[dict], node) -> str:
    """One category per table for the charts: the strongest claim wins."""
    if node.kind == KIND_MODEL_TABLE:
        return "Semantic model"
    if node.kind == KIND_REPORT_VISUAL:
        return "Report"
    kinds = [p["kind"] for p in producers]
    for kind in (
        "Mirroring",
        "Shortcut",
        "Pipeline",
        "Copy job",
        "Dataflow Gen2",
        "Notebook",
        "Spark job",
    ):
        if kind in kinds:
            return kind
    return kinds[0] if kinds else "Unknown"


def open_findings(
    context: Context, limit: int = 20, scope: set[str] | None = None
) -> list[dict]:
    """
    Open findings, worst first.

    Ordered by severity then score so "PII readable by 38 people" outranks
    "PII readable by 2", which is the order someone triaging would want.
    """
    severity_rank = {"critical": 0, "serious": 1, "warning": 2}
    stmt = select(Finding).where(
        Finding.customer_id == context.customer_id,
        Finding.tenant_id == context.tenant_id,
        Finding.resolved_at.is_(None),
    )
    rows = sorted(
        (
            f
            for f in context.session.scalars(stmt)
            if scope is None
            or f.node_id in scope
            or (f.item_id and any(context.nodes[n].item_id == f.item_id for n in scope))
        ),
        key=lambda f: (severity_rank.get(f.severity, 3), -f.score),
    )
    out = []
    for finding in rows[:limit]:
        out.append(
            {
                "type": finding.findingType
                if hasattr(finding, "findingType")
                else finding.finding_type,
                "severity": finding.severity,
                "tone": _finding_tone(finding.finding_type),
                "title": finding.message,
                "detail": _finding_detail(finding),
                "nodeId": finding.node_id,
                "itemId": finding.item_id,
                "tab": _finding_tab(finding.finding_type),
            }
        )
    return out


def _finding_tone(finding_type: str) -> str:
    return {
        "stale": "Stale",
        "pii_exposure": "PII",
        "undocumented": "Docs",
        "indirect_access": "Access",
        "orphan": "Orphan",
        "unresolved_lineage": "Lineage",
    }.get(finding_type, finding_type.title())


def _finding_tab(finding_type: str) -> str:
    """Which tab answers this finding, so a click lands somewhere useful."""
    return {
        "pii_exposure": "access",
        "indirect_access": "access",
        "undocumented": "columns",
        "unresolved_lineage": "code",
        "orphan": "lineage",
    }.get(finding_type, "overview")


def _finding_detail(finding: Finding) -> str:
    details = finding.details or {}
    if finding.finding_type == "pii_exposure":
        columns = ", ".join(details.get("columns", [])[:3])
        return f"Columns: {columns}."
    if finding.finding_type == "stale":
        return f"{details.get('hoursLate', 0)} hours later than its usual cadence."
    if finding.finding_type == "undocumented":
        return (
            f"{details.get('columnsTotal', 0) - details.get('columnsDocumented', 0)}"
            f" of {details.get('columnsTotal', 0)} columns have no description."
        )
    if finding.finding_type == "indirect_access":
        return "; ".join(details.get("paths", [])[:1])
    if finding.finding_type == "unresolved_lineage":
        return "; ".join(details.get("warnings", [])[:1])
    return ""


def _recent(context: Context) -> list[dict]:
    rows = delta_collector.recently_written(
        context.session, context.customer_id, context.tenant_id
    )
    out = []
    for row in rows:
        node = context.nodes.get(row["nodeId"])
        if node is None:
            continue
        written_by = _producer_label(context, node.id) or row.get("engine") or ""
        out.append(
            {
                "nodeId": node.id,
                "name": context.label(node.id),
                "by": written_by,
                "operation": row["operation"],
                "committedAt": row["committedAt"].isoformat()
                if row["committedAt"]
                else None,
            }
        )
    return out


# --- graph ----------------------------------------------------------------
def graph(
    context: Context,
    *,
    focus: str | None = None,
    up: int = 2,
    down: int = 2,
    select_expr: str = "",
    workspace: str = "",
    item: str = "",
    table: str = "",
    q: str = "",
) -> dict:
    """
    Nodes and edges for the graph view.

    With a focus, returns its neighbourhood and marks the full upstream/downstream
    path so the UI can highlight it and dim the rest. With a selector, returns
    what the selector matched. With neither, the whole estate.
    """
    adjacency = context.adjacency
    all_nodes = list(context.nodes.values())
    scope = scope_nodes(context, workspace=workspace, item=item, table=table, q=q)

    if scope is not None and not (focus and focus in context.nodes):
        # A workspace / item / table filter: those nodes, and everything that
        # feeds them or reads from them — where the data comes from and goes.
        node_ids = set(scope)
        for seed in scope:
            node_ids |= graph_module.full_paths(adjacency, seed)
        highlighted = set(scope)
        if select_expr:
            tags = _tags_by_node(context)
            node_ids &= selectors.resolve(
                select_expr, all_nodes, adjacency, tags_by_node=tags
            )
    elif focus and focus in context.nodes:
        node_ids, _depths = graph_module.neighbourhood(adjacency, focus, up=up, down=down)
        highlighted = graph_module.full_paths(adjacency, focus)
    elif select_expr:
        tags = _tags_by_node(context)
        node_ids = selectors.resolve(select_expr, all_nodes, adjacency, tags_by_node=tags)
        highlighted = set(node_ids)
    else:
        node_ids = {n.id for n in all_nodes}
        highlighted = set()

    edges = graph_module.edges_between(adjacency, node_ids)
    lanes = _lanes(context, node_ids)

    return {
        "focus": focus,
        "lanes": lanes,
        "nodes": [
            {
                **node_brief(context, node_id),
                "isFocus": node_id == focus,
                "onPath": node_id in highlighted,
            }
            for node_id in sorted(node_ids, key=lambda n: _lane_index(context, n))
        ],
        "edges": [
            {
                "from": e.from_node_id,
                "to": e.to_node_id,
                "viaItem": _item_name(context, e.via_item_id),
                "viaRef": e.via_ref,
                "writeMode": e.write_mode,
                "source": e.source,
                "confidence": e.confidence,
                "isDynamic": e.is_dynamic,
                "onPath": e.from_node_id in highlighted and e.to_node_id in highlighted,
            }
            for e in edges
        ],
    }


def _lanes(context: Context, node_ids: set[str]) -> list[dict]:
    """
    The lanes to draw, in order, with their members.

    Empty lanes are kept: a graph with no semantic model still shows the lane so
    the layout does not shift when one appears.
    """
    members: dict[str, list[str]] = defaultdict(list)
    for node_id in node_ids:
        node = context.nodes.get(node_id)
        if node is not None:
            members[node.layer].append(node_id)
    return [
        {
            "layer": layer,
            "label": LAYER_LABELS.get(layer, layer.title()),
            "nodeIds": sorted(members.get(layer, []), key=lambda n: context.label(n)),
        }
        for layer in LAYERS
    ]


def _lane_index(context: Context, node_id: str) -> int:
    node = context.nodes.get(node_id)
    return LAYERS.index(node.layer) if node and node.layer in LAYERS else 0


def _tags_by_node(context: Context) -> dict[str, list[str]]:
    """Tags per node, unioned from its columns plus its docs."""
    out: dict[str, list[str]] = defaultdict(list)
    for column in context.session.scalars(
        select(ColumnNode).where(
            ColumnNode.customer_id == context.customer_id,
            ColumnNode.tenant_id == context.tenant_id,
        )
    ):
        for tag in column.tags or []:
            if tag not in out[column.node_id]:
                out[column.node_id].append(tag)
        if column.is_pii and "PII" not in out[column.node_id]:
            out[column.node_id].append("PII")
    return out


def _item_name(context: Context, item_id: str | None) -> str:
    item = context.items.get(item_id or "")
    return item.display_name if item else ""


# --- node page ------------------------------------------------------------
def node_page(context: Context, node_id: str) -> dict | None:
    """Header, details list and the direct upstream/downstream lists."""
    node = context.nodes.get(node_id)
    if node is None:
        return None

    docs = repository.resolve_docs(
        context.session, context.customer_id, context.tenant_id, [node_id]
    )
    doc = docs.get(node_id)
    upstream, downstream = graph_module.direct(context.adjacency, node_id)

    columns = repository.list_columns(
        context.session, context.customer_id, context.tenant_id, node_id
    )
    tags = []
    if any(c.is_pii for c in columns):
        tags.append({"label": "PII", "tone": "danger"})
    if columns and not any((c.description or "").strip() for c in columns):
        tags.append({"label": "Undocumented", "tone": "warn"})
    for tag in (doc.tags if doc else []) or []:
        tags.append({"label": tag, "tone": "neutral"})

    return {
        **node_brief(context, node_id),
        "description": (doc.description if doc else "")
        or (
            NO_DESCRIPTION_POWER_BI
            if node.kind in (KIND_REPORT_VISUAL, KIND_MODEL_TABLE)
            else NO_DESCRIPTION
        ),
        "hasDescription": bool(doc and (doc.description or "").strip()),
        "descriptionMeta": _doc_meta(doc),
        "tags": tags,
        "details": _details(context, node),
        "upstream": [node_brief(context, n) for n in upstream],
        "downstream": [node_brief(context, n) for n in downstream],
        "layerIsDeclared": node.layer_is_declared,
        **fabric_link(context, context.items.get(node.item_id or "")),
    }


# Where an item opens in the Fabric portal, by its type. Anything else opens
# its workspace.
_FABRIC_PATH = {
    "Lakehouse": "lakehouses",
    "Warehouse": "datawarehouses",
    "MirroredDatabase": "mirroreddatabases",
    "Notebook": "synapsenotebooks",
    "DataPipeline": "pipelines",
    "Dataflow": "dataflows-gen2",
    "SparkJobDefinition": "sparkjobdefinitions",
    "Eventstream": "eventstreams",
    "SemanticModel": "datasets",
    "Report": "reports",
}


def fabric_link(context: Context, item: Item | None) -> dict:
    """
    `fabricUrl`: the item in the Fabric portal, in the customer's own tenant
    (`ctid`), for "Open in Fabric". Empty on the demo tenant, whose ids are
    invented (`fabricDemo` says so, so the button can explain).
    """
    tenant = context.tenant
    workspace = context.workspaces.get(item.workspace_id) if item else None
    if item is None or workspace is None or not workspace.workspace_guid:
        return {"fabricUrl": "", "fabricDemo": False}
    if tenant is None or tenant.is_demo:
        return {"fabricUrl": "", "fabricDemo": True}
    base = f"https://app.fabric.microsoft.com/groups/{workspace.workspace_guid}"
    path = _FABRIC_PATH.get(item.item_type)
    url = f"{base}/{path}/{item.item_guid}" if path and item.item_guid else f"{base}/list"
    if tenant.azure_tenant_id:
        url += f"?ctid={tenant.azure_tenant_id}"
    return {"fabricUrl": url, "fabricDemo": False}


def _details(context: Context, node: DataNode) -> list[dict]:
    """
    The Details list on the Overview tab.

    Only facts we actually hold: an absent row is better than "unknown", which
    reads as a failure rather than as something this tier does not collect.
    """
    session = context.session
    rows: list[tuple[str, str]] = []

    if node.kind == KIND_EXTERNAL:
        detail = node.connection_detail or {}
        for key, label in (
            ("server", "Server"),
            ("database", "Database"),
            ("url", "URL"),
            ("kind", "Connection type"),
        ):
            if detail.get(key):
                rows.append((label, str(detail[key])))
        discovered = _discovered_from(context, node.id)
        if discovered:
            rows.append(("Discovered from", discovered))
        return [{"key": k, "value": v} for k, v in rows]

    producer = _producer_label(context, node.id)
    if producer:
        rows.append(("Produced by", producer))
    orchestrator = _orchestrator_label(context, node.id)
    if orchestrator:
        rows.append(("Orchestrated by", orchestrator))

    item = context.items.get(node.item_id or "")
    if node.kind == KIND_MODEL_TABLE and item is not None:
        if item.storage_mode:
            rows.append(("Storage mode", item.storage_mode))
        if item.uses_fixed_identity:
            rows.append(("Connection identity", "Fixed identity"))
        if item.endorsement:
            rows.append(("Endorsement", item.endorsement))

    stats = session.get(DeltaStats, node.id)
    if stats is not None:
        if stats.last_write_at:
            rows.append(("Last write", f"{stats.last_write_at:%d %b %Y %H:%M}"))
        mode = _last_write_mode(session, context.tenant_id, node.id)
        if mode:
            rows.append(("Write mode", mode))
        if stats.num_rows is not None:
            rows.append(("Rows", f"{stats.num_rows:,}"))
        if stats.size_bytes:
            rows.append(("Size", _human_bytes(stats.size_bytes)))
        version_count = session.scalar(
            select(DeltaVersion.id)
            .where(
                DeltaVersion.tenant_id == context.tenant_id,
                DeltaVersion.node_id == node.id,
            )
            .order_by(DeltaVersion.version.desc())
            .limit(1)
        )
        if stats.current_version is not None:
            rows.append(("Delta versions", f"{stats.current_version + 1:,}"))
        elif version_count:
            rows.append(("Delta versions", "—"))
        if stats.last_optimize_at:
            rows.append(("Last OPTIMIZE", f"{stats.last_optimize_at:%d %b %Y %H:%M}"))

    docs = repository.resolve_docs(
        session, context.customer_id, context.tenant_id, [node.id]
    )
    doc = docs.get(node.id)
    if doc and doc.owner:
        rows.append(("Owner", doc.owner))

    return [{"key": k, "value": v} for k, v in rows]


def _producer_label(context: Context, node_id: str) -> str:
    """
    "Notebook · nb_gold_model › cell 2" — the item and the exact part of it.

    Several items can write one table; all are listed rather than one being
    silently chosen, because "which of these two notebooks wrote it" is exactly
    the question someone debugging is asking.
    """
    edges = [
        e for e in context.adjacency.edges if e.to_node_id == node_id and e.via_item_id
    ]
    labels: list[str] = []
    for edge in edges:
        item = context.items.get(edge.via_item_id or "")
        if item is None:
            continue
        label = f"{_item_type_label(item)} · {item.display_name}"
        if edge.via_ref:
            label += f" › {edge.via_ref}"
        if label not in labels:
            labels.append(label)
    return "; ".join(labels)


def _orchestrator_label(context: Context, node_id: str) -> str:
    """
    The pipeline(s) that run the item writing this table, from parsed
    invocations — `pl_master › pl_load_sales` when one pipeline invokes
    another — with the orchestrator's last run from the job history.

    Walks upward from the writer through whatever invokes it (a pipeline, or a
    notebook calling `notebook.run`) to the nearest pipeline. No invocation
    found means no claim: a guessed orchestrator sends someone to the wrong place.
    """
    from sqlalchemy import select

    from .models import ItemInvocation, JobRun

    writers = {
        e.via_item_id
        for e in context.adjacency.edges
        if e.to_node_id == node_id and e.via_item_id
    }
    if not writers:
        return ""
    callers: dict[str, set[str]] = {}
    for from_id, to_id in context.session.execute(
        select(ItemInvocation.from_item_id, ItemInvocation.to_item_id).where(
            ItemInvocation.tenant_id == context.tenant_id
        )
    ):
        callers.setdefault(to_id, set()).add(from_id)

    def is_pipeline(item_id: str) -> bool:
        item = context.items.get(item_id)
        return item is not None and item.item_type == "DataPipeline"

    def chain(pipeline_id: str) -> list[str]:
        """Parent pipelines above this one, outermost first (max 5 levels)."""
        path, current, seen = [pipeline_id], pipeline_id, {pipeline_id}
        for _ in range(5):
            parents = sorted(p for p in callers.get(current, ()) if is_pipeline(p))
            if len(parents) != 1 or parents[0] in seen:
                break
            current = parents[0]
            seen.add(current)
            path.insert(0, current)
        return path

    # Nearest pipelines above each writer, through non-pipeline callers too.
    found: list[str] = []
    frontier, seen = set(writers), set(writers)
    for _ in range(5):
        next_frontier: set[str] = set()
        for item_id in frontier:
            for caller in callers.get(item_id, ()):
                if caller in seen:
                    continue
                seen.add(caller)
                if is_pipeline(caller) and caller not in writers:
                    found.append(caller)
                else:
                    next_frontier.add(caller)
        if found or not next_frontier:
            break
        frontier = next_frontier

    labels = []
    for pipeline_id in sorted(
        set(found), key=lambda i: context.items[i].display_name.lower()
    ):
        names = " › ".join(context.items[i].display_name for i in chain(pipeline_id))
        run = context.session.scalars(
            select(JobRun)
            .where(JobRun.item_id == pipeline_id)
            .order_by(JobRun.started_at.desc())
            .limit(1)
        ).first()
        if run is not None and run.started_at is not None:
            names += f" (last run {run.started_at:%d %b %H:%M}"
            names += f", {run.status})" if run.status else ")"
        labels.append(names)
    return "; ".join(labels)


def _discovered_from(context: Context, node_id: str) -> str:
    edges = [e for e in context.adjacency.edges if e.from_node_id == node_id]
    for edge in edges:
        item = context.items.get(edge.via_item_id or "")
        if item is not None:
            return (
                f"{item.display_name} · {edge.via_ref}"
                if edge.via_ref
                else item.display_name
            )
    return ""


def _item_type_label(item: Item) -> str:
    return {
        "Notebook": "Notebook",
        "DataPipeline": "Pipeline",
        "Dataflow": "Dataflow Gen2",
        "SemanticModel": "Semantic model",
        "Report": "Report",
    }.get(item.item_type, item.item_type)


def _last_write_mode(session: Session, tenant_id: str, node_id: str) -> str:
    from ..fabric import deltalog

    row = session.scalars(
        select(DeltaVersion)
        .where(DeltaVersion.tenant_id == tenant_id, DeltaVersion.node_id == node_id)
        .order_by(DeltaVersion.version.desc())
        .limit(5)
    ).first()
    if row is None:
        return ""
    operation = row.operation or ""
    mode = deltalog.write_mode_from(
        {"operation": operation, "operation_parameters": row.operation_parameters}
    )
    if mode == "merge":
        keys = (row.operation_parameters or {}).get("predicate") or ""
        return f"MERGE{f' ({keys[:60]})' if keys else ''}"
    return mode.upper() if mode else operation


def _human_bytes(value: int) -> str:
    step = 1024.0
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < step:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} PB"


# --- descriptions written in FabriBuddy -------------------------------------
_DOC_SOURCE_LABELS = {
    DOC_USER: "Written in FabriBuddy",
    DOC_TMDL: "From the semantic model (TMDL)",
    DOC_DELTA_PROPERTY: "From the Delta table properties",
}


def _doc_meta(doc) -> dict | None:
    """Where a description came from and, for one written here, who last edited it."""
    if doc is None or not (doc.description or "").strip():
        return None
    return {
        "source": doc.source,
        "sourceLabel": _DOC_SOURCE_LABELS.get(doc.source, doc.source),
        "updatedBy": doc.updated_by if doc.source == DOC_USER else "",
        "updatedAt": doc.updated_at.isoformat() if doc.updated_at else None,
    }


def set_description(
    context: "Context", *, node_id: str, column: str | None, text: str, editor: str
) -> bool:
    """
    Store (or, with empty text, clear) a description written in FabriBuddy.

    Kept in its own row with source DOC_USER, so the collected description is
    never overwritten and returns if this one is cleared. False when the column
    does not exist.
    """
    from sqlalchemy import delete

    column_id = None
    if column is not None:
        match = next(
            (
                c
                for c in repository.list_columns(
                    context.session, context.customer_id, context.tenant_id, node_id
                )
                if c.name.lower() == column.lower()
            ),
            None,
        )
        if match is None:
            return False
        column_id = match.id
    target_node = None if column_id else node_id
    text = text.strip()
    if not text:
        context.session.execute(
            delete(Doc).where(
                Doc.customer_id == context.customer_id,
                Doc.tenant_id == context.tenant_id,
                Doc.source == DOC_USER,
                *(
                    (Doc.column_id == column_id,)
                    if column_id
                    else (Doc.node_id == node_id, Doc.column_id.is_(None))
                ),
            )
        )
    else:
        repository.upsert_doc(
            context.session,
            context.customer_id,
            context.tenant_id,
            node_id=target_node,
            column_id=column_id,
            source=DOC_USER,
            description=text,
            updated_by=editor[:320],
        )
    context.session.commit()
    return True


# --- columns --------------------------------------------------------------
def node_columns(context: Context, node_id: str) -> list[dict]:
    columns = repository.list_columns(
        context.session, context.customer_id, context.tenant_id, node_id
    )
    docs = repository.resolve_docs(
        context.session,
        context.customer_id,
        context.tenant_id,
        [c.id for c in columns],
    )
    out = []
    for column in columns:
        doc = docs.get(column.id)
        description = (doc.description if doc else "") or column.description or ""
        out.append(
            {
                "id": column.id,
                "name": column.name,
                "type": column.data_type,
                "nullable": column.nullable,
                "description": description,
                "hasDescription": bool(description.strip()),
                "descriptionMeta": _doc_meta(doc)
                if doc and (doc.description or "").strip()
                else {"source": "delta_property", "updatedBy": "", "updatedAt": None}
                if description.strip()
                else None,
                "tags": list(column.tags or []),
                "isPii": column.is_pii,
                "isMeasure": column.is_measure,
            }
        )
    return out


# --- code -----------------------------------------------------------------
def node_code(context: Context, node_id: str) -> dict:
    """
    The code that writes a node, one entry per producing item, with the exact
    place highlighted.

    When no code is available the reason is specific — which credential tier or
    which label is in the way — because "no code" alone tells the customer
    nothing they can act on.
    """
    node = context.nodes.get(node_id)
    if node is None:
        return {"available": False, "reason": "Unknown node."}

    edges = [
        e for e in context.adjacency.edges if e.to_node_id == node_id and e.via_item_id
    ]
    # One definition per (item, place): four source tables read by one cell are
    # one piece of code, not four.
    seen: set[tuple[str, str]] = set()
    unique = []
    for edge in edges:
        key = (edge.via_item_id or "", edge.via_ref or "")
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    if not unique:
        return {
            "available": False,
            "reason": (
                "No defining item found. Run the definitions collector, or this "
                "table may be written from outside Fabric."
            ),
        }

    definitions = [_definition(context, node, edge) for edge in unique]
    available = [d for d in definitions if d.get("available")]
    if not available:
        return definitions[0]
    return {**available[0], "definitions": available}


def _definition(context: Context, node, edge) -> dict:
    item = context.items.get(edge.via_item_id or "")
    if item is None:
        return {"available": False, "reason": "The defining item is no longer present."}

    artifacts = list(
        context.session.scalars(
            select(CodeArtifact).where(
                CodeArtifact.tenant_id == context.tenant_id,
                CodeArtifact.item_id == item.id,
            )
        )
    )
    if not artifacts:
        workspace = context.workspaces.get(item.workspace_id)
        if workspace is not None and not workspace.has_contributor:
            reason = (
                f"Code unavailable — the service principal is not Contributor on "
                f"{workspace.name}."
            )
        elif item.sensitivity_label:
            reason = "Code unavailable — protected sensitivity label."
        else:
            reason = "Code has not been collected for this item yet."
        return {"available": False, "reason": reason, "item": item.display_name}

    named = next(
        (a for a in artifacts if edge.via_ref and a.part_path == edge.via_ref), None
    )
    artifact = named or _pick_artifact(artifacts, item.item_type)
    highlight = _highlight_range(artifact, edge.via_ref, item.item_type, node.table_name)

    return {
        "available": True,
        "definedIn": (
            f"{item.display_name} · {edge.via_ref}" if edge.via_ref else item.display_name
        ),
        "item": item.display_name,
        "itemType": item.item_type,
        "writeMode": edge.write_mode,
        "partPath": artifact.part_path,
        "language": artifact.language,
        "detectedVia": _detected_via(edge, item.item_type),
        "text": artifact.text,
        "highlight": highlight,
        "fetchedAt": artifact.fetched_at.isoformat() if artifact.fetched_at else None,
        "fetchMode": artifact.fetch_mode,
    }


def _pick_artifact(artifacts: list[CodeArtifact], item_type: str) -> CodeArtifact:
    preferred = {
        "Notebook": "notebook-content.py",
        "DataPipeline": "pipeline-content.json",
        "Dataflow": ".pq",
        "SemanticModel": ".tmdl",
        "Report": "visual.json",
    }.get(item_type, "")
    if preferred:
        for artifact in artifacts:
            if preferred in artifact.part_path:
                return artifact
    return artifacts[0]


def _detected_via(edge: LineageEdge, item_type: str) -> str:
    by_source = {
        "runtime": "Runtime (OpenLineage run event)",
        "declared": "Declared in the item definition",
        "inferred": "Inferred — not parsed from code",
    }
    if edge.source == "inferred" and item_type == "Dataflow":
        return (
            "Inferred — the Delta log shows Dataflow Gen2 wrote this table, and this "
            "dataflow's query of that name has loading switched off today"
        )
    if edge.source in by_source:
        return by_source[edge.source]
    return {
        "Notebook": "Static parse (Spark SQL via sqlglot / PySpark AST)",
        "DataPipeline": "Pipeline definition (getDefinition)",
        "Dataflow": "Dataflow Gen2 definition (mashup.pq)",
        "SemanticModel": "Scanner API + model definition (TMDL)",
        "Report": "Report definition (PBIR)",
    }.get(item_type, "Static parse")


_WRITE_MARKERS = (
    "saveastable",
    ".save(",
    "insertinto",
    ".merge(",
    "insert into",
    "insert overwrite",
    "merge into",
    "create table",
    "create or replace table",
    "replace table",
)


def _highlight_range(
    artifact: CodeArtifact, via_ref: str, item_type: str, table: str = ""
) -> dict | None:
    """
    Where in the artifact this table is written.

    Returns {startLine, endLine, lines}: the block (a notebook cell, a dataflow
    query, a pipeline activity) plus the exact lines inside it that do the
    write, when they can be told apart. A wrong highlight is worse than none —
    it sends the reader to the wrong code — so anything unidentifiable is None.
    """
    text = artifact.text or ""
    if item_type == "Notebook" and via_ref.startswith("cell "):
        block = _notebook_cell(text, via_ref)
    elif item_type == "Dataflow" and via_ref.startswith("query "):
        block = _dataflow_query(text, via_ref[len("query ") :])
    elif item_type == "DataPipeline" and via_ref:
        block = _json_object_named(text, via_ref)
    elif via_ref.endswith(".sql"):
        # A warehouse module or statement: the artifact is the statement.
        block = (1, max(1, text.rstrip("\n").count("\n") + 1))
    else:
        block = None
    if block is None:
        return None
    start, end = block
    lines = _write_lines(text, start, end, table)
    if item_type == "Notebook" and not any(_is_write(text, n) for n in lines):
        # The block is where the write is *triggered* (a helper called in a
        # loop); the write itself may live in the helper's cell. Mark a write
        # line anywhere whose name template fits this table.
        lines = _templated_writes(text, table) or lines
    return {"startLine": start, "endLine": end, "lines": lines}


def _is_write(text: str, number: int) -> bool:
    line = text.split("\n")[number - 1].lower()
    return any(marker in line for marker in _WRITE_MARKERS)


def _templated_writes(text: str, table: str) -> list[int]:
    """
    Write lines whose name template fits the table.

    `saveAsTable(f"projops.mapping_{entity}_{attr}")`: the table part of the
    template, `mapping_{…}_{…}`, has the literal pieces `mapping_` and `_`,
    all of which occur in `mapping_msdyn_actual_msdyn_billingtype`.
    """
    import re

    name = (table or "").lower()
    if not name:
        return []
    out = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not any(marker in line.lower() for marker in _WRITE_MARKERS):
            continue
        for literal in re.findall(r"[\"']([^\"']+)[\"']", line):
            if "{" not in literal:
                continue
            # The last dot-separated part (outside braces) is the table.
            table_part = re.split(r"\.(?![^{]*\})", literal.lower())[-1]
            pieces = [p for p in re.split(r"\{[^}]*\}", table_part) if p]
            if pieces and name.startswith(pieces[0]) and all(p in name for p in pieces):
                out.append(number)
                break
    return out


def _write_lines(text: str, start: int, end: int, table: str) -> list[int]:
    """Lines in the block naming the table, else the lines that perform a write."""
    lines = text.split("\n")[start - 1 : end]
    needle = (table or "").lower()
    named = [start + i for i, line in enumerate(lines) if needle and needle in line.lower()]
    writes = [
        start + i
        for i, line in enumerate(lines)
        if any(marker in line.lower() for marker in _WRITE_MARKERS)
    ]
    both = [n for n in named if n in writes]
    return both or writes or named


def _notebook_cell(text: str, via_ref: str) -> tuple[int, int] | None:
    try:
        wanted = int(via_ref.split()[1])
    except (IndexError, ValueError):
        return None
    from .parsers.notebook import split_cells

    cells, _ = split_cells(text)
    for cell in cells:
        if cell.index != wanted:
            continue
        position = text.find(cell.text)
        if position < 0:
            return None
        start = text.count("\n", 0, position) + 1
        return start, start + cell.text.rstrip("\n").count("\n")
    return None


def _dataflow_query(text: str, query: str) -> tuple[int, int] | None:
    """`shared <query> = …;` plus the destination attribute right above it."""
    import re

    pattern = re.compile(
        r"^[ \t]*shared\s+(?:#\"" + re.escape(query) + r"\"|" + re.escape(query) + r")\s*=",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    lines = text.split("\n")
    start = text.count("\n", 0, match.start()) + 1
    first = start
    while first > 1 and lines[first - 2].lstrip().startswith("["):
        first -= 1  # [DataDestinations = …] / [BindToDefaultDestination = true]
    # A section member ends at its `;` — M uses it for nothing else.
    end = start
    while end < len(lines) and not lines[end - 1].rstrip().endswith(";"):
        end += 1
    return first, end


def _json_object_named(text: str, name: str) -> tuple[int, int] | None:
    """The JSON object whose `"name"` is `name` — a pipeline activity."""
    import json

    marker = f'"name": {json.dumps(name)}'
    at = text.find(marker)
    if at < 0:
        marker = f'"name":{json.dumps(name)}'
        at = text.find(marker)
    if at < 0:
        return None
    depth, open_at = 0, -1
    for i in range(at, -1, -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                open_at = i
                break
            depth -= 1
    if open_at < 0:
        return None
    depth, close_at, in_string = 0, -1, False
    for i in range(open_at, len(text)):
        ch = text[i]
        if in_string:
            if ch == "\\":
                continue
            if ch == '"' and text[i - 1] != "\\":
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                close_at = i
                break
    if close_at < 0:
        return None
    return text.count("\n", 0, open_at) + 1, text.count("\n", 0, close_at) + 1


# --- access ---------------------------------------------------------------
def node_access(context: Context, node_id: str) -> dict:
    return access_collector.summarise_for_node(
        context.session, context.customer_id, context.tenant_id, node_id
    )


# --- column trace ---------------------------------------------------------
def column_trace(context: Context, column_id: str) -> dict | None:
    """
    The full upstream and downstream column set, grouped by lane.

    Includes the transform at each hop, which is the thing that makes a column
    trace worth having: knowing `net_amount_eur` comes from `LINEAMOUNT` is
    half an answer without `LINEAMOUNT - LINEDISC` then `* rate_to_eur`.
    """
    try:
        node_id, column_name = ids.split_column_id(column_id)
    except ValueError:
        return None
    if node_id not in context.nodes:
        return None

    adjacency = context.column_adjacency
    upstream, downstream = graph_module.trace_column(adjacency, column_id)
    every = {column_id} | upstream | downstream

    columns = graph_module.load_columns(
        context.session, context.customer_id, context.tenant_id, every
    )

    lanes: dict[str, list[dict]] = defaultdict(list)
    for member in every:
        member_node_id, member_column = (
            ids.split_column_id(member) if ids.COLUMN_SEP in member else (member, "")
        )
        node = context.nodes.get(member_node_id)
        if node is None:
            continue
        column = columns.get(member)
        lanes[node.layer].append(
            {
                "columnId": member,
                "nodeId": member_node_id,
                "table": context.label(member_node_id),
                "column": member_column or (column.name if column else ""),
                "transform": graph_module.incoming_transform(adjacency, member),
                "isPii": bool(column.is_pii) if column else False,
                "isSelected": member == column_id,
            }
        )

    ordered = [
        {
            "layer": layer,
            "label": LAYER_LABELS.get(layer, layer.title()),
            "items": sorted(lanes[layer], key=lambda i: (i["table"], i["column"])),
        }
        for layer in LAYERS
        if lanes.get(layer)
    ]

    upstream_tables = {ids.split_column_id(c)[0] for c in upstream if ids.COLUMN_SEP in c}
    selected = columns.get(column_id)
    note = _trace_note(selected, len(downstream), context.nodes[node_id].kind)

    return {
        "columnId": column_id,
        "column": column_name,
        "nodeId": node_id,
        "table": context.label(node_id),
        "lanes": ordered,
        "summary": (
            f"{len(upstream)} upstream column{'s' if len(upstream) != 1 else ''} in "
            f"{len(upstream_tables)} item{'s' if len(upstream_tables) != 1 else ''} · "
            f"{len(downstream)} downstream column{'s' if len(downstream) != 1 else ''}"
        ),
        "note": note,
    }


def _trace_note(column: ColumnNode | None, downstream_count: int, kind: str) -> str:
    """
    The banner above a trace.

    The PII case is the one worth stating outright: a PII column that stops at
    silver is good news and deserves to be said, not left for the reader to
    infer from an empty lane.
    """
    if column is not None and column.is_pii:
        if downstream_count == 0:
            return (
                "PII column. It is not propagated further downstream, so gold "
                "and reports are clean."
            )
        return (
            f"PII column. It propagates to {downstream_count} downstream "
            f"column{'s' if downstream_count != 1 else ''} — check access on each."
        )
    if downstream_count == 0 and kind != KIND_REPORT_VISUAL:
        return "No downstream consumers. Either unused, or the lineage is not captured yet."
    return ""


# --- search ---------------------------------------------------------------
def search(context: Context, query: str, limit: int = 40) -> dict:
    """
    Across node names, column names, descriptions and code.

    Code search is last and capped: a full-text scan of every notebook in a large
    tenant is slow, and someone typing a table name wants the table first.
    """
    needle = (query or "").strip()
    if len(needle) < 2:
        return {"nodes": [], "columns": [], "code": []}

    pattern = f"%{needle.lower()}%"
    session = context.session

    node_hits = [
        node_brief(context, n.id)
        for n in context.nodes.values()
        if needle.lower() in (n.table_name or "").lower()
        or needle.lower() in (n.display_path or "").lower()
    ][:limit]

    column_rows = session.scalars(
        select(ColumnNode)
        .where(
            ColumnNode.customer_id == context.customer_id,
            ColumnNode.tenant_id == context.tenant_id,
            or_(
                ColumnNode.name.ilike(pattern),
                ColumnNode.description.ilike(pattern),
            ),
        )
        .limit(limit)
    )
    column_hits = [
        {
            "columnId": c.id,
            "nodeId": c.node_id,
            "name": c.name,
            "table": context.label(c.node_id),
            "description": c.description,
            "isPii": c.is_pii,
        }
        for c in column_rows
        if c.node_id in context.nodes
    ]

    code_rows = session.scalars(
        select(CodeArtifact)
        .where(
            CodeArtifact.customer_id == context.customer_id,
            CodeArtifact.tenant_id == context.tenant_id,
            CodeArtifact.text.ilike(pattern),
        )
        .limit(limit)
    )
    code_hits = []
    for artifact in code_rows:
        item = context.items.get(artifact.item_id)
        if item is None:
            continue
        code_hits.append(
            {
                "itemId": item.id,
                "item": item.display_name,
                "itemType": item.item_type,
                "partPath": artifact.part_path,
                "snippet": _snippet(artifact.text, needle),
            }
        )

    return {"nodes": node_hits, "columns": column_hits, "code": code_hits}


def _snippet(text: str, needle: str, width: int = 120) -> str:
    position = text.lower().find(needle.lower())
    if position < 0:
        return text[:width]
    start = max(0, position - width // 2)
    end = min(len(text), position + width // 2)
    return (
        ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    )


# --- an item page: a notebook, pipeline, dataflow… on its own ---------------------------
def item_page(context: Context, item_id: str) -> dict | None:
    """
    One code item with its code, what it writes and reads, and what the parser
    could not resolve — reachable even when none of its writes resolved, which
    is exactly when someone needs to read it.
    """
    from ..models import CollectorError, CollectorRun

    item = context.items.get(item_id)
    if item is None:
        return None
    workspace = context.workspaces.get(item.workspace_id)
    artifacts = list(
        context.session.scalars(
            select(CodeArtifact)
            .where(
                CodeArtifact.tenant_id == context.tenant_id, CodeArtifact.item_id == item.id
            )
            .order_by(CodeArtifact.part_path)
        )
    )

    writes: dict[str, dict] = {}
    reads: dict[str, dict] = {}
    for edge in context.adjacency.edges:
        if edge.via_item_id != item.id:
            continue
        for node_id, bucket in ((edge.to_node_id, writes), (edge.from_node_id, reads)):
            node = context.nodes.get(node_id)
            if node is None or node_id in bucket:
                continue
            bucket[node_id] = {
                "id": node_id,
                "name": node.display_path or node.table_name,
                "kind": node.kind,
                "mode": edge.write_mode if bucket is writes else "",
                "confidence": edge.confidence,
                "where": edge.via_ref,
            }
    # A table this item both reads and writes (a MERGE) is a write.
    for node_id in writes:
        reads.pop(node_id, None)

    latest_parse = context.session.scalar(
        select(CollectorRun.id)
        .where(CollectorRun.tenant_id == context.tenant_id, CollectorRun.kind == "parse")
        .order_by(CollectorRun.started_at.desc())
        .limit(1)
    )
    warnings = (
        [
            e.message
            for e in context.session.scalars(
                select(CollectorError).where(
                    CollectorError.run_id == latest_parse,
                    CollectorError.scope_id == item.id,
                )
            )
        ]
        if latest_parse
        else []
    )

    parsed = _parsed_now(context, item) if artifacts else []

    # A lakehouse or warehouse lists the tables it holds and the items that
    # write them; code of its own (procedures, mirroring) is shown as well.
    tables: list[dict] = []
    written_by: dict[str, dict] = {}
    if item.item_type in STORAGE_ITEM_TYPES:
        held = sorted(
            (
                n
                for n in context.nodes.values()
                if n.item_id == item.id and n.kind in STORAGE_KINDS
            ),
            key=lambda n: (n.schema_name or "", n.table_name or ""),
        )
        held_ids = {n.id for n in held}
        tables = [
            {
                "id": n.id,
                "name": _table_name(n),
                "kind": n.kind,
                "mode": "",
                "confidence": "high",
                "where": "",
            }
            for n in held
        ]
        for edge in context.adjacency.edges:
            via = context.items.get(edge.via_item_id or "")
            if edge.to_node_id not in held_ids or via is None:
                continue
            # A warehouse's own stored procedures, a mirrored database's
            # mirroring and a lakehouse's shortcuts write through the item itself.
            own = via.id == item.id
            written_by.setdefault(
                via.id,
                {
                    "id": via.id,
                    "name": _OWN_WRITER.get(via.item_type, via.display_name)
                    if own
                    else via.display_name,
                    "type": _PRODUCER_KIND.get(via.item_type, via.item_type),
                    "own": own,
                },
            )

    reason = ""
    if not artifacts and item.item_type not in STORAGE_ITEM_TYPES:
        if workspace is not None and not workspace.has_contributor:
            reason = (
                "Code unavailable — the service principal is not Contributor on "
                f"{workspace.name}."
            )
        elif item.sensitivity_label:
            reason = "Code unavailable — protected sensitivity label."
        else:
            reason = "Code has not been collected for this item yet."

    variables: list[dict] = []
    used_by: list[dict] = []
    active_value_set = ""
    if item.item_type == "VariableLibrary":
        from .collectors.definitions import ACTIVE_VALUE_SET_PART
        from .parsers import mirroring as mirroring_parser

        parts = {a.part_path: a.text for a in artifacts}
        active_value_set = parts.pop(ACTIVE_VALUE_SET_PART, "")
        variables = mirroring_parser.variable_table(parts, active_value_set)
        artifacts = [a for a in artifacts if a.part_path != ACTIVE_VALUE_SET_PART]
        used_by = _library_users(context, item)

    return {
        "id": item.id,
        "name": item.display_name,
        "type": item.item_type,
        "variables": variables,
        "activeValueSet": active_value_set,
        "usedBy": used_by,
        "storage": item.item_type in STORAGE_ITEM_TYPES,
        "typeLabel": STORAGE_ITEM_TYPES.get(item.item_type)
        or _PRODUCER_KIND.get(item.item_type)
        or {"VariableLibrary": "Variable library"}.get(item.item_type, item.item_type),
        "path": " / ".join(
            p for p in ((workspace.name if workspace else ""), item.display_name) if p
        ),
        "description": item.description or "",
        "artifacts": [
            {
                "partPath": a.part_path,
                "language": a.language,
                "text": a.text,
                "fetchedAt": a.fetched_at.isoformat() if a.fetched_at else None,
            }
            for a in artifacts
        ],
        "codeUnavailable": reason,
        "parsed": parsed,
        "writes": sorted(writes.values(), key=lambda r: r["name"].lower()),
        "reads": sorted(reads.values(), key=lambda r: r["name"].lower()),
        "warnings": warnings,
        "tables": tables,
        "writtenBy": sorted(written_by.values(), key=lambda r: r["name"].lower()),
        **fabric_link(context, item),
    }


def _library_users(context: Context, library) -> list[dict]:
    """
    Items in the library's workspace whose code names it — notebookutils'
    getLibrary("lib"), a pipeline's libraryVariables, a dataflow's
    Variable.ValueOrDefault("$(/**/lib/var)").
    """
    name = library.display_name
    needles = (f'"{name}"', f"'{name}'", f"/{name}/")
    users: dict[str, dict] = {}
    for artifact in context.session.scalars(
        select(CodeArtifact).where(CodeArtifact.tenant_id == context.tenant_id)
    ):
        item = context.items.get(artifact.item_id)
        if item is None or item.id == library.id or item.id in users:
            continue
        if item.workspace_id != library.workspace_id:
            continue
        if any(n in (artifact.text or "") for n in needles):
            users[item.id] = {
                "id": item.id,
                "name": item.display_name,
                "type": item.item_type,
            }
    return sorted(users.values(), key=lambda u: u["name"].lower())


def _ref_label(ref) -> str:
    """How a parsed table reference reads to a person."""
    if ref.dynamic_expr:
        return f"(dynamic) {ref.dynamic_expr}"
    url = (ref.connection or {}).get("url") or ""
    if url or ref.path:
        return (
            f"{url.rstrip('/')}{ref.path}"
            if url and ref.path.startswith("/")
            else (url or ref.path)
        )
    return ".".join(p for p in (ref.lakehouse, ref.schema, ref.table) if p) or "(unknown)"


def _parsed_now(context: Context, item) -> list[dict]:
    """
    What the code says, read again now: each write target with the sources it
    reads, and whether lineage could pin the target to a table. Parsers are pure
    and fast, so this costs a fraction of a second and never goes stale.
    """
    from .collectors import definitions as definitions_collector

    items = context.items
    artifacts_by_item: dict[str, list] = {}
    for artifact in context.session.scalars(
        select(CodeArtifact).where(CodeArtifact.tenant_id == context.tenant_id)
    ):
        artifacts_by_item.setdefault(artifact.item_id, []).append(artifact)
    try:
        from types import SimpleNamespace

        parse_context = definitions_collector._Context.build(
            items,
            context.workspaces,
            artifacts_by_item,
            SimpleNamespace(nodes=list(context.nodes.values())),
        )
        result, _extra = definitions_collector._run_parser(
            item, artifacts_by_item.get(item.id, []), parse_context
        )
    except Exception as e:  # noqa: BLE001 — a display aid, never an error page
        log.warning("re-parse for the item page failed: %s", e)
        return []
    if result is None:
        return []
    resolved_targets = {
        e.via_ref for e in context.adjacency.edges if e.via_item_id == item.id
    }
    out = []
    for write in result.writes:
        out.append(
            {
                "target": _ref_label(write.table),
                "mode": write.mode,
                "where": write.via_ref,
                "reads": [_ref_label(r) for r in write.reads],
                "resolved": write.via_ref in resolved_targets,
            }
        )
    return out
