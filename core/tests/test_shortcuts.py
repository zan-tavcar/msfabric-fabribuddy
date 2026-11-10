"""Shortcut lineage: table shortcuts and whole-schema shortcuts. Synthetic ids only."""

from types import SimpleNamespace

from fabribuddy_core.lineage import ids
from fabribuddy_core.lineage.collectors import items
from fabribuddy_core.lineage.models import KIND_LAKEHOUSE_TABLE

SRC_WS, SRC_LH = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
DST_WS, DST_LH = "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"


def _run(monkeypatch, shortcuts, src_tables, dst_tables):
    """Run _shortcut_edges against an in-memory node set; return the edges made."""
    nodes = {}
    for ws, lh, tables in ((SRC_WS, SRC_LH, src_tables), (DST_WS, DST_LH, dst_tables)):
        for schema, table in tables:
            node_id = ids.lakehouse_table_id(ws, lh, schema, table)
            nodes[node_id] = SimpleNamespace(
                id=node_id,
                kind=KIND_LAKEHOUSE_TABLE,
                item_id=ids.item_id(ws, lh),
                schema_name=schema,
                table_name=table,
            )
    edges = []
    repo = SimpleNamespace(
        get_node=lambda s, c, t, node_id: nodes.get(node_id),
        list_nodes=lambda s, c, t, kind=None, item_id=None, **_: [
            n for n in nodes.values()
            if (kind is None or n.kind == kind) and (item_id is None or n.item_id == item_id)
        ],
        upsert_node=lambda s, c, t, **kw: nodes.setdefault(kw["id"], SimpleNamespace(**kw)),
        upsert_edge=lambda s, c, t, **kw: edges.append(kw),
    )
    monkeypatch.setattr(items, "repository", repo)
    monkeypatch.setattr(items.items_api, "list_shortcuts", lambda *a: shortcuts)
    session = SimpleNamespace(flush=lambda: None)
    made, errors = items._shortcut_edges(
        session, None, "cust", "tenant",
        ws_guid=DST_WS, ws_name="dst", lh_guid=DST_LH, lh_name="lh",
    )
    assert not errors and made == len(edges)
    return {(e["from_node_id"], e["to_node_id"]) for e in edges}


def _onelake(path):
    return {"type": "OneLake", "oneLake": {"workspaceId": SRC_WS, "itemId": SRC_LH, "path": path}}


def test_table_shortcut_links_to_its_source(monkeypatch):
    edges = _run(
        monkeypatch,
        [{"path": "Tables/dbo", "name": "orders", "target": _onelake("Tables/sales/orders")}],
        src_tables=[("sales", "orders")],
        dst_tables=[("dbo", "orders")],
    )
    assert edges == {(
        ids.lakehouse_table_id(SRC_WS, SRC_LH, "sales", "orders"),
        ids.lakehouse_table_id(DST_WS, DST_LH, "dbo", "orders"),
    )}


def test_schema_shortcut_links_every_table_to_its_source(monkeypatch):
    edges = _run(
        monkeypatch,
        [{"path": "Tables", "name": "bronze", "target": _onelake("Tables/sales")}],
        src_tables=[("sales", "orders"), ("sales", "customers")],
        dst_tables=[("bronze", "orders"), ("bronze", "customers"), ("dbo", "local")],
    )
    assert edges == {
        (
            ids.lakehouse_table_id(SRC_WS, SRC_LH, "sales", t),
            ids.lakehouse_table_id(DST_WS, DST_LH, "bronze", t),
        )
        for t in ("orders", "customers")
    }


def test_schema_shortcut_to_a_lakehouse_without_schemas(monkeypatch):
    edges = _run(
        monkeypatch,
        [{"path": "Tables", "name": "bronze", "target": _onelake("Tables")}],
        src_tables=[("dbo", "orders")],
        dst_tables=[("bronze", "orders")],
    )
    assert edges == {(
        ids.lakehouse_table_id(SRC_WS, SRC_LH, "dbo", "orders"),
        ids.lakehouse_table_id(DST_WS, DST_LH, "bronze", "orders"),
    )}


def test_schema_shortcut_out_of_scope_links_each_table_to_an_external_node(monkeypatch):
    edges = _run(
        monkeypatch,
        [{"path": "Tables", "name": "bronze", "target": _onelake("Tables/sales")}],
        src_tables=[],  # source workspace not scanned
        dst_tables=[("bronze", "orders"), ("bronze", "customers")],
    )
    sources = {src for src, _ in edges}
    assert len(edges) == 2 and len(sources) == 2
    assert all(src.startswith("ext:") for src in sources)
