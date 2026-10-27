"""
Database access for the lineage graph.

Same rule as app/repository.py: customer_id and tenant_id are mandatory on
every function, so isolation is structural rather than remembered.

Writes are upserts keyed on the deterministic ids from ids.py — a re-scan
updates in place and stamps `last_seen_at`, which is also how a table that
disappeared is detected (seen-before, not-seen-now) without deleting its
history.
"""

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from ..models import utcnow
from .models import (
    ColumnEdge,
    ColumnNode,
    DataNode,
    Doc,
    Item,
    LineageEdge,
    Workspace,
)

# SQLite refuses a statement with more than 32,766 bound variables, and a
# model-heavy tenant has tens of thousands of nodes. Every IN over a caller-sized
# id set goes through this, in chunks well under the limit.
IN_CHUNK = 900


def chunks(ids, size: int = IN_CHUNK):
    ids = list(ids)
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


# --- same-pass lookups -----------------------------------------------------
# Sessions run with autoflush=False, so neither session.get() nor a select can
# see a row added earlier in the same pass. Without this index, a key seen twice
# in one collection (a workspace in two scanner batches, a column and measure
# sharing a name) becomes two INSERTs and the final commit fails on the PK.
_PENDING = "fl_pending"


def _pending(session: Session) -> dict:
    return session.info.setdefault(_PENDING, {})


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _clear_pending(session: Session) -> None:
    # After a commit the rows are in the identity map; after a rollback they are
    # gone. Either way the index would only hold stale references.
    session.info.pop(_PENDING, None)


def forget_pending(session: Session) -> None:
    """After a savepoint rollback: the rows it held are gone (no session event says so)."""
    session.info.pop(_PENDING, None)


class TenantCollision(Exception):
    """A deterministic id already belongs to another organisation or tenant."""


def _owned(row, customer_id: str, tenant_id: str):
    """
    Refuse to update another tenant's row.

    Ids are deterministic and primary keys are global; an id that two tenants
    could both produce must never let one tenant's scan change the other's data.
    """
    if row is not None and (row.customer_id != customer_id or row.tenant_id != tenant_id):
        raise TenantCollision(
            f"{type(row).__name__} {row.id} belongs to another tenant; not updated."
        )
    return row


def _get(session: Session, cls, id: str):
    return session.get(cls, id) or _pending(session).get((cls, id))


def _find(session: Session, cls, key: tuple, stmt):
    return _pending(session).get((cls, key)) or session.scalars(stmt).first()


def _add(session: Session, row, key) -> None:
    session.add(row)
    _pending(session)[(type(row), key)] = row


# --- workspaces ------------------------------------------------------------
def upsert_workspace(
    session: Session, customer_id: str, tenant_id: str, *, id: str, **fields
) -> Workspace:
    row = _owned(_get(session, Workspace, id), customer_id, tenant_id)
    if row is None:
        row = Workspace(id=id, customer_id=customer_id, tenant_id=tenant_id, **fields)
        _add(session, row, id)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    row.last_seen_at = utcnow()
    return row


def delete_tenant_data(session: Session, customer_id: str, tenant_id: str) -> None:
    """
    Remove everything collected for one tenant.

    Lineage tables key on tenant_id without a foreign key to connected_tenant
    (they are written by collectors, not owned by the connection row), so
    deleting the connection alone would leave the tenant's whole estate behind.
    Children first, so the order holds even where cascades are off.
    """
    from sqlalchemy import delete

    from ..db import Base

    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "connected_tenant" or "tenant_id" not in table.c:
            continue
        condition = table.c.tenant_id == tenant_id
        if "customer_id" in table.c:
            condition = condition & (table.c.customer_id == customer_id)
        session.execute(delete(table).where(condition))
    session.commit()


def purge_orphaned_tenant_data(session: Session) -> int:
    """Delete data whose tenant no longer exists. Returns how many tenants it held."""
    from ..models import ConnectedTenant

    live = {t.id for t in session.scalars(select(ConnectedTenant))}
    orphans = [
        (customer_id, tenant_id)
        for customer_id, tenant_id in session.execute(
            select(Workspace.customer_id, Workspace.tenant_id).distinct()
        )
        if tenant_id not in live
    ]
    for customer_id, tenant_id in orphans:
        delete_tenant_data(session, customer_id, tenant_id)
    return len(orphans)


def purge_workspaces_outside(
    session: Session, customer_id: str, tenant_id: str, keep_guids: set[str]
) -> int:
    """
    Delete every workspace outside the SPN's Contributor scope, with all it holds.

    A hard delete, unlike a table that vanished (which keeps its Delta history):
    an out-of-scope workspace is one this product must not document at all, not
    one whose history is evidence. Items, nodes, columns, edges, artifacts,
    grants and findings go with it through the ON DELETE CASCADE foreign keys.
    """
    from sqlalchemy import delete

    keep = {g.lower() for g in keep_guids}
    doomed = [
        w.id
        for w in list_workspaces(session, customer_id, tenant_id)
        if w.workspace_guid.lower() not in keep
    ]
    for part in chunks(doomed):
        session.execute(
            delete(Workspace).where(
                Workspace.customer_id == customer_id,
                Workspace.tenant_id == tenant_id,
                Workspace.id.in_(part),
            )
        )
    session.commit()
    if doomed:
        # Sources only the removed workspaces read are now islands.
        prune_island_externals(session, customer_id, tenant_id)
    return len(doomed)


def prune_island_externals(session: Session, customer_id: str, tenant_id: str) -> int:
    """
    Delete external sources no lineage edge touches any more.

    An external node exists only because some code read it; once that code is
    gone (out of scope, rewritten) it connects to nothing and is noise in the
    sidebar, the graph and the catalog. Parsing re-creates it if code reads it
    again.
    """
    from sqlalchemy import delete, union

    from .models import KIND_EXTERNAL

    connected = union(
        select(LineageEdge.from_node_id).where(LineageEdge.tenant_id == tenant_id),
        select(LineageEdge.to_node_id).where(LineageEdge.tenant_id == tenant_id),
    )
    result = session.execute(
        delete(DataNode)
        .where(
            DataNode.customer_id == customer_id,
            DataNode.tenant_id == tenant_id,
            DataNode.kind == KIND_EXTERNAL,
            DataNode.id.not_in(connected),
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount or 0


def list_workspaces(session: Session, customer_id: str, tenant_id: str) -> list[Workspace]:
    stmt = (
        select(Workspace)
        .where(Workspace.customer_id == customer_id, Workspace.tenant_id == tenant_id)
        .order_by(Workspace.name)
    )
    return list(session.scalars(stmt))


# --- items -----------------------------------------------------------------
def upsert_item(
    session: Session, customer_id: str, tenant_id: str, *, id: str, **fields
) -> Item:
    row = _owned(_get(session, Item, id), customer_id, tenant_id)
    if row is None:
        row = Item(id=id, customer_id=customer_id, tenant_id=tenant_id, **fields)
        _add(session, row, id)
    else:
        for key, value in fields.items():
            # A later collector must not blank a field an earlier one filled:
            # the Fabric items API has no sensitivity label, and overwriting
            # the scanner's value with "" would lose it every other run.
            if value in ("", None) and getattr(row, key, None):
                continue
            setattr(row, key, value)
    row.last_seen_at = utcnow()
    return row


def get_item(session: Session, customer_id: str, tenant_id: str, item_id: str):
    stmt = select(Item).where(
        Item.customer_id == customer_id,
        Item.tenant_id == tenant_id,
        Item.id == item_id,
    )
    return session.scalars(stmt).first()


def list_items(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    workspace_id: str | None = None,
    item_type: str | None = None,
) -> list[Item]:
    stmt = select(Item).where(Item.customer_id == customer_id, Item.tenant_id == tenant_id)
    if workspace_id:
        stmt = stmt.where(Item.workspace_id == workspace_id)
    if item_type:
        stmt = stmt.where(Item.item_type == item_type)
    return list(session.scalars(stmt.order_by(Item.display_name)))


def count_items_by_type(
    session: Session, customer_id: str, tenant_id: str
) -> dict[str, int]:
    stmt = (
        select(Item.item_type, func.count(Item.id))
        .where(Item.customer_id == customer_id, Item.tenant_id == tenant_id)
        .group_by(Item.item_type)
    )
    return {row[0]: row[1] for row in session.execute(stmt)}


# --- nodes -----------------------------------------------------------------
def upsert_node(
    session: Session, customer_id: str, tenant_id: str, *, id: str, **fields
) -> DataNode:
    row = _owned(_get(session, DataNode, id), customer_id, tenant_id)
    if row is None:
        row = DataNode(id=id, customer_id=customer_id, tenant_id=tenant_id, **fields)
        _add(session, row, id)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    row.last_seen_at = utcnow()
    # A table that came back after being marked gone is not a new node.
    row.is_deleted = False
    return row


def get_node(session: Session, customer_id: str, tenant_id: str, node_id: str):
    stmt = select(DataNode).where(
        DataNode.customer_id == customer_id,
        DataNode.tenant_id == tenant_id,
        DataNode.id == node_id,
    )
    return session.scalars(stmt).first()


def list_nodes(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    kind: str | None = None,
    layer: str | None = None,
    item_id: str | None = None,
    include_deleted: bool = False,
) -> list[DataNode]:
    stmt = select(DataNode).where(
        DataNode.customer_id == customer_id, DataNode.tenant_id == tenant_id
    )
    if not include_deleted:
        stmt = stmt.where(DataNode.is_deleted.is_(False))
    if kind:
        stmt = stmt.where(DataNode.kind == kind)
    if layer:
        stmt = stmt.where(DataNode.layer == layer)
    if item_id:
        stmt = stmt.where(DataNode.item_id == item_id)
    return list(session.scalars(stmt.order_by(DataNode.display_path)))


def mark_missing_nodes(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    item_id: str,
    seen_ids: set[str],
) -> int:
    """
    Soft-delete nodes under one item that this pass did not see.

    Scoped to a single item on purpose: a collector that only managed to read
    three of a tenant's ten lakehouses must not conclude the other seven are
    gone. The caller only calls this for items it actually enumerated.
    """
    stmt = select(DataNode).where(
        DataNode.customer_id == customer_id,
        DataNode.tenant_id == tenant_id,
        DataNode.item_id == item_id,
        DataNode.is_deleted.is_(False),
    )
    n = 0
    for node in session.scalars(stmt):
        if node.id not in seen_ids:
            node.is_deleted = True
            n += 1
    return n


# --- columns ---------------------------------------------------------------
def upsert_column(
    session: Session, customer_id: str, tenant_id: str, *, id: str, **fields
) -> ColumnNode:
    row = _owned(_get(session, ColumnNode, id), customer_id, tenant_id)
    if row is None:
        row = ColumnNode(id=id, customer_id=customer_id, tenant_id=tenant_id, **fields)
        _add(session, row, id)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    return row


def list_columns(
    session: Session, customer_id: str, tenant_id: str, node_id: str
) -> list[ColumnNode]:
    stmt = (
        select(ColumnNode)
        .where(
            ColumnNode.customer_id == customer_id,
            ColumnNode.tenant_id == tenant_id,
            ColumnNode.node_id == node_id,
        )
        .order_by(ColumnNode.ordinal, ColumnNode.name)
    )
    return list(session.scalars(stmt))


# --- edges -----------------------------------------------------------------
def upsert_edge(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    from_node_id: str,
    to_node_id: str,
    via_item_id: str | None = None,
    via_ref: str = "",
    **fields,
) -> LineageEdge | None:
    """
    Add or strengthen one table-level edge.

    A stronger `source` overwrites a weaker one (runtime > declared > parsed >
    inferred) but never the reverse — otherwise a re-parse would downgrade an
    edge that a runtime observation had already confirmed.
    """
    from .models import EDGE_SOURCE_RANK

    if from_node_id == to_node_id:
        # A MERGE reading and writing the same table is real, but as an edge it
        # is a self-loop that breaks layout and says nothing useful.
        return None

    stmt = select(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id,
        LineageEdge.from_node_id == from_node_id,
        LineageEdge.to_node_id == to_node_id,
        LineageEdge.via_item_id == via_item_id,
        LineageEdge.via_ref == via_ref,
    )
    key = (tenant_id, from_node_id, to_node_id, via_item_id, via_ref)
    row = _find(session, LineageEdge, key, stmt)
    if row is None:
        row = LineageEdge(
            customer_id=customer_id,
            tenant_id=tenant_id,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            via_item_id=via_item_id,
            via_ref=via_ref,
            **fields,
        )
        _add(session, row, key)
        return row

    incoming = fields.get("source", row.source)
    if EDGE_SOURCE_RANK.get(incoming, 0) >= EDGE_SOURCE_RANK.get(row.source, 0):
        for key, value in fields.items():
            setattr(row, key, value)
        row.detected_at = utcnow()
    return row


def upsert_column_edge(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    from_column_id: str,
    to_column_id: str,
    via_item_id: str | None = None,
    via_ref: str = "",
    **fields,
) -> ColumnEdge | None:
    from .models import EDGE_SOURCE_RANK

    if from_column_id == to_column_id:
        return None
    stmt = select(ColumnEdge).where(
        ColumnEdge.tenant_id == tenant_id,
        ColumnEdge.from_column_id == from_column_id,
        ColumnEdge.to_column_id == to_column_id,
        ColumnEdge.via_item_id == via_item_id,
        ColumnEdge.via_ref == via_ref,
    )
    key = (tenant_id, from_column_id, to_column_id, via_item_id, via_ref)
    row = _find(session, ColumnEdge, key, stmt)
    if row is None:
        row = ColumnEdge(
            customer_id=customer_id,
            tenant_id=tenant_id,
            from_column_id=from_column_id,
            to_column_id=to_column_id,
            via_item_id=via_item_id,
            via_ref=via_ref,
            **fields,
        )
        _add(session, row, key)
        return row
    incoming = fields.get("source", row.source)
    if EDGE_SOURCE_RANK.get(incoming, 0) >= EDGE_SOURCE_RANK.get(row.source, 0):
        for key, value in fields.items():
            setattr(row, key, value)
    return row


def edges_for(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    node_ids: set[str] | None = None,
) -> list[LineageEdge]:
    stmt = select(LineageEdge).where(
        LineageEdge.customer_id == customer_id, LineageEdge.tenant_id == tenant_id
    )
    if node_ids is None:
        return list(session.scalars(stmt))
    found: dict[int, LineageEdge] = {}
    for part in chunks(node_ids):
        chunk_stmt = stmt.where(
            LineageEdge.from_node_id.in_(part) | LineageEdge.to_node_id.in_(part)
        )
        for edge in session.scalars(chunk_stmt):
            found[edge.id] = edge  # an edge spanning two chunks comes back twice
    return list(found.values())


def clear_parsed_edges(
    session: Session, customer_id: str, tenant_id: str, item_id: str
) -> None:
    """
    Drop the edges a previous parse of this item produced, before re-parsing it.

    Only `parsed` ones: a re-parse is the authority on what the code says, so
    an edge the old parser drew wrongly must go — but a declared edge (a
    shortcut, a mirroring definition) or a runtime observation is not the
    parser's to retract.
    """
    from sqlalchemy import delete

    from .models import SOURCE_PARSED

    for model in (ColumnEdge, LineageEdge):
        session.execute(
            delete(model).where(
                model.customer_id == customer_id,
                model.tenant_id == tenant_id,
                model.via_item_id == item_id,
                model.source == SOURCE_PARSED,
            )
        )


def prune_star_columns(session: Session, customer_id: str, tenant_id: str) -> int:
    """
    Remove `*` / `alias.*` columns an older parser created from a star projection.

    A star is not a column; the resolver now pairs it by name instead. Only
    untyped rows (made from code, never read from a schema) that no edge still
    references are removed.
    """
    from sqlalchemy import delete, or_

    used = (
        select(ColumnEdge.from_column_id)
        .where(ColumnEdge.tenant_id == tenant_id)
        .union(select(ColumnEdge.to_column_id).where(ColumnEdge.tenant_id == tenant_id))
    )
    result = session.execute(
        delete(ColumnNode)
        .where(
            ColumnNode.customer_id == customer_id,
            ColumnNode.tenant_id == tenant_id,
            ColumnNode.data_type == "",
            or_(ColumnNode.name == "*", ColumnNode.name.like("%.*")),
            ColumnNode.id.not_in(used),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


# --- docs ------------------------------------------------------------------
def upsert_doc(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    node_id: str | None,
    column_id: str | None,
    source: str,
    **fields,
) -> Doc:
    stmt = select(Doc).where(
        Doc.tenant_id == tenant_id,
        Doc.node_id.is_(node_id) if node_id is None else Doc.node_id == node_id,
        Doc.column_id.is_(column_id) if column_id is None else Doc.column_id == column_id,
        Doc.source == source,
    )
    key = (tenant_id, node_id, column_id, source)
    row = _find(session, Doc, key, stmt)
    if row is None:
        row = Doc(
            customer_id=customer_id,
            tenant_id=tenant_id,
            node_id=node_id,
            column_id=column_id,
            source=source,
            **fields,
        )
        _add(session, row, key)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
    return row


def resolve_docs(
    session: Session, customer_id: str, tenant_id: str, targets: list[str]
) -> dict[str, Doc]:
    """
    Best description per target, preferring a human edit over collected metadata.

    Returns a map keyed by node_id or column_id. Callers pass whichever set of
    ids they are rendering, so one query serves a whole table of columns.
    """
    from .models import DOC_SOURCE_RANK

    if not targets:
        return {}
    docs = []
    for part in chunks(targets):
        stmt = select(Doc).where(
            Doc.customer_id == customer_id,
            Doc.tenant_id == tenant_id,
            Doc.node_id.in_(part) | Doc.column_id.in_(part),
        )
        docs.extend(session.scalars(stmt))
    best: dict[str, Doc] = {}
    for doc in docs:
        key = doc.column_id or doc.node_id
        if key is None:
            continue
        current = best.get(key)
        if current is None or DOC_SOURCE_RANK.get(doc.source, 0) >= DOC_SOURCE_RANK.get(
            current.source, 0
        ):
            best[key] = doc
    return best
