"""
The sidebar hierarchy.

workspace -> item type -> lakehouse -> schema -> table
                       -> notebook / pipeline / dataflow -> the tables it writes
                       -> semantic model
                       -> report
plus an "External sources" group pinned at the bottom, grouped by connection.

Built in one pass over three queries rather than per-node lookups: a tenant
with 2,000 tables would otherwise issue 2,000 round trips to render a sidebar.
"""

from sqlalchemy.orm import Session

from . import repository
from .models import KIND_EXTERNAL, STORAGE_ITEM_TYPES, STORAGE_KINDS

# Item types that get their own branch in the tree, in display order.
BRANCH_TYPES = (
    "Lakehouse",
    "Warehouse",
    "MirroredDatabase",
    "SQLDatabase",
    "Notebook",
    "DataPipeline",
    "Dataflow",
    "SparkJobDefinition",
    "CopyJob",
    "Eventstream",
    "SemanticModel",
    "Report",
    "VariableLibrary",
)

# Items whose code writes tables. They hold no tables themselves, so each lists
# the tables it writes — a compute workspace full of notebooks is where the
# estate is built, and must not vanish from the tree for holding no data.
CODE_TYPES = (
    "Notebook",
    "DataPipeline",
    "Dataflow",
    "SparkJobDefinition",
    "CopyJob",
    "Eventstream",
)

# The folder each type sits in. A workspace with 200 reports shows one
# "Reports (200)" row instead of pushing its lakehouses off the screen.
TYPE_LABELS = {
    "Lakehouse": "Lakehouses",
    "Warehouse": "Warehouses",
    "MirroredDatabase": "Mirrored databases",
    "SQLDatabase": "SQL databases",
    "Notebook": "Notebooks",
    "DataPipeline": "Pipelines",
    "Dataflow": "Dataflows",
    "SparkJobDefinition": "Spark jobs",
    "CopyJob": "Copy jobs",
    "Eventstream": "Eventstreams",
    "SemanticModel": "Semantic models",
    "Report": "Reports",
    "VariableLibrary": "Variable libraries",
}

EXTERNAL_GROUP_ID = "group:external"


def build(session: Session, customer_id: str, tenant_id: str) -> dict:
    """Return the full tree. Counts are included so the UI never has to derive them."""
    workspaces = repository.list_workspaces(session, customer_id, tenant_id)
    items = repository.list_items(session, customer_id, tenant_id)
    nodes = repository.list_nodes(session, customer_id, tenant_id)

    items_by_ws: dict[str, list] = {}
    for item in items:
        items_by_ws.setdefault(item.workspace_id, []).append(item)

    written_by: dict[str, list] = _written_by(session, customer_id, tenant_id, nodes)

    nodes_by_item: dict[str, list] = {}
    external: list = []
    for node in nodes:
        if node.kind == KIND_EXTERNAL:
            external.append(node)
        elif node.item_id:
            nodes_by_item.setdefault(node.item_id, []).append(node)

    ws_branches = []
    for ws in workspaces:
        by_type: dict[str, list] = {}
        for item in sorted(
            items_by_ws.get(ws.id, []), key=lambda i: i.display_name.lower()
        ):
            if item.item_type in CODE_TYPES:
                by_type.setdefault(item.item_type, []).append(
                    _code_branch(item, written_by.get(item.id, []))
                )
            elif item.item_type in BRANCH_TYPES:
                by_type.setdefault(item.item_type, []).append(
                    _item_branch(item, nodes_by_item.get(item.id, []))
                )
        children = [
            {
                "id": f"{ws.id}:type:{item_type}",
                "kind": "itemtype",
                "itemType": item_type,
                "name": TYPE_LABELS[item_type],
                "count": len(by_type[item_type]),
                "children": by_type[item_type],
            }
            for item_type in BRANCH_TYPES
            if by_type.get(item_type)
        ]

        # A workspace with neither data nor code that writes it is noise in a
        # lineage tree — a Power BI workspace that happens to exist.
        if not children:
            continue
        ws_branches.append(
            {
                "id": ws.id,
                "kind": "workspace",
                "name": ws.name,
                "hasContributor": ws.has_contributor,
                "count": sum(c["count"] for c in children),
                "children": children,
            }
        )

    columns = _columns(session, customer_id, tenant_id)
    for branch in ws_branches:
        _attach_columns(branch, columns)

    tree = {
        "workspaces": ws_branches,
        "external": _external_group(external),
        "counts": {
            "workspaces": len(ws_branches),
            "items": len(items),
            "tables": sum(1 for n in nodes if n.kind in STORAGE_KINDS),
            "external": len(external),
        },
    }
    return tree


def _item_branch(item, nodes: list) -> dict:
    """
    One item. A lakehouse or warehouse gets a schema level; everything else
    lists its nodes directly (a model's tables); a report is a single leaf.
    """
    branch = {
        "id": item.id,
        "kind": item.item_type,
        "name": item.display_name,
        "sensitivityLabel": item.sensitivity_label,
        "endorsement": item.endorsement,
    }

    if item.item_type in STORAGE_ITEM_TYPES:
        schemas: dict[str, list] = {}
        for node in nodes:
            schemas.setdefault(node.schema_name or "dbo", []).append(node)
        branch["children"] = [
            {
                "id": f"{item.id}:{schema}",
                "kind": "schema",
                "name": schema,
                "count": len(members),
                "children": [_leaf(n) for n in sorted(members, key=_by_name)],
            }
            for schema, members in sorted(schemas.items())
        ]
        branch["count"] = len(nodes)
    elif item.item_type == "Report" and nodes:
        # A report is one node bound to one model: the row itself opens it,
        # never a folder (older builds left a node per visual behind).
        whole = [n for n in nodes if n.id.endswith(":report")]
        return {**_leaf((whole or nodes)[0]), "name": item.display_name}
    elif item.item_type == "VariableLibrary":
        # Holds values, not tables: the row opens the library's own page.
        branch["children"] = []
        branch["count"] = 0
    elif item.item_type == "SemanticModel" and len(nodes) == 1:
        # A semantic model is one node: the row itself opens it.
        return _leaf(nodes[0])
    else:
        branch["children"] = [_leaf(n) for n in sorted(nodes, key=_by_name)]
        branch["count"] = len(nodes)

    return branch


def _written_by(session: Session, customer_id: str, tenant_id: str, nodes: list) -> dict:
    """item id -> the storage tables its code writes (one query, not one per item)."""
    from sqlalchemy import select

    from .models import LineageEdge

    by_id = {n.id: n for n in nodes if n.kind in STORAGE_KINDS}
    out: dict[str, dict] = {}
    for via, target in session.execute(
        select(LineageEdge.via_item_id, LineageEdge.to_node_id).where(
            LineageEdge.customer_id == customer_id,
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.via_item_id.is_not(None),
        )
    ):
        node = by_id.get(target)
        if node is not None:
            out.setdefault(via, {})[node.id] = node
    return {item: list(found.values()) for item, found in out.items()}


def _code_branch(item, written: list) -> dict:
    """A notebook, pipeline or dataflow, with the tables it writes as leaves."""
    return {
        "id": item.id,
        "kind": item.item_type,
        "name": item.display_name,
        "sensitivityLabel": item.sensitivity_label,
        "endorsement": item.endorsement,
        "children": [_leaf(n) for n in sorted(written, key=_by_name)],
        "count": len(written),
    }


def _columns(session: Session, customer_id: str, tenant_id: str) -> dict[str, list]:
    """Each table's columns as [name, type] pairs, in table order."""
    from sqlalchemy import select

    from .models import ColumnNode

    out: dict[str, list] = {}
    for node_id, name, data_type in session.execute(
        select(ColumnNode.node_id, ColumnNode.name, ColumnNode.data_type)
        .where(ColumnNode.customer_id == customer_id, ColumnNode.tenant_id == tenant_id)
        .order_by(ColumnNode.node_id, ColumnNode.ordinal, ColumnNode.name)
    ):
        out.setdefault(node_id, []).append([name, data_type or ""])
    return out


def _attach_columns(branch: dict, columns: dict[str, list]) -> None:
    """
    Storage tables carry their columns, so the sidebar can fold a table open
    and its search finds columns. Compact pairs rather than tree nodes: an
    estate can hold tens of thousands of columns, and the client unfolds them.
    """
    if branch.get("kind") in STORAGE_KINDS:
        cols = columns.get(branch["id"])
        if cols:
            branch["columns"] = cols
        return
    for child in branch.get("children") or []:
        _attach_columns(child, columns)


def _leaf(node) -> dict:
    return {
        "id": node.id,
        "kind": node.kind,
        "name": node.table_name or node.display_path,
        "layer": node.layer,
        "count": 0,
        "children": [],
    }


def _by_name(node) -> str:
    return (node.table_name or "").lower()


def _external_group(external: list) -> dict:
    """
    External sources, grouped by connection.

    Grouping by `connection_hash` is what keeps one SQL Server with forty
    tables from filling the sidebar with forty top-level entries.
    """
    by_connection: dict[str, list] = {}
    for node in external:
        by_connection.setdefault(node.connection_hash or "unknown", []).append(node)

    children = []
    for connection_hash, members in sorted(by_connection.items()):
        detail = members[0].connection_detail or {}
        label = (
            detail.get("server")
            or detail.get("url")
            or detail.get("path")
            or detail.get("kind")
            or "Unknown source"
        )
        children.append(
            {
                "id": f"conn:{connection_hash}",
                "kind": "connection",
                "name": label,
                "count": len(members),
                "children": [_leaf(n) for n in sorted(members, key=_by_name)],
            }
        )

    return {
        "id": EXTERNAL_GROUP_ID,
        "kind": "group",
        "name": "External sources",
        "count": len(external),
        "children": children,
    }
