"""
Turning parser output into graph rows.

The parsers are deliberately ignorant of the tenant: they report what the code
*says* (`spark.read.table("lh_bronze.d365.custtable")`) and this module decides
which node that is. Three things happen here that a parser cannot do:

1. **Name -> node id.** A lakehouse is named in code but identified by GUID in
   the database. The lookup is by (workspace, lakehouse name, schema, table),
   falling back to a tenant-wide name match when the writing item lives in a
   different workspace from the lakehouse it writes.

2. **Candidate filtering.** A join makes the parser emit one column edge per
   possible source. Here we know the real schema, so a candidate whose source
   table has no such column is dropped. This is what turns low-confidence
   guesses into correct lineage.

3. **External node creation.** A source that is not a lakehouse table becomes an
   `external` node keyed on its connection fingerprint, so one SQL Server
   reached from forty activities is one node with forty tables.
"""

import logging
import re

from sqlalchemy.orm import Session

from .. import config
from . import ids, repository
from .models import (
    CONF_LOW,
    CONF_MEDIUM,
    KIND_EXTERNAL,
    KIND_MODEL_TABLE,
    KIND_REPORT_VISUAL,
    LAYER_SOURCE,
    SOURCE_PARSED,
    ColumnNode,
    DataNode,
    Item,
)
from .parsers.base import ColumnEdgeRef, ParseResult, TableRef, Write

log = logging.getLogger("lineage.resolve")

# The Fabric API says "SQLEndpoint"; the Scanner API says "SQLAnalyticsEndpoint".
SQL_ENDPOINT_TYPES = {"SQLEndpoint", "SQLAnalyticsEndpoint"}

_GUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# abfss://<ws>@onelake.dfs.fabric.microsoft.com/<item>/<rest>
# https://onelake.dfs.fabric.microsoft.com/<ws>/<item>/<rest>  (also blob.)
_ONELAKE_URL = re.compile(
    r"^(?:abfss://(?P<ws>[^@/]+)@[^/]*onelake[^/]*/"
    r"|https://[^/]*onelake[^/]*/(?P<ws2>[^/]+)/)"
    r"(?P<item>[^/]+)/(?P<rest>.*)$",
    re.IGNORECASE,
)


class Resolver:
    """
    Resolves parsed table references against one tenant's inventory.

    Built once per collector run: it loads the tenant's tables and columns up
    front because a per-reference query would be one round trip per line of
    parsed code.
    """

    def __init__(self, session: Session, customer_id: str, tenant_id: str):
        self.session = session
        self.customer_id = customer_id
        self.tenant_id = tenant_id

        self.nodes: list[DataNode] = repository.list_nodes(session, customer_id, tenant_id)
        self.items: list[Item] = repository.list_items(session, customer_id, tenant_id)

        # (lakehouse name, schema, table) -> node, for qualified references.
        self._by_full: dict[tuple[str, str, str], DataNode] = {}
        # (schema, table) -> nodes, for references that name no lakehouse.
        self._by_schema_table: dict[tuple[str, str], list[DataNode]] = {}
        # table -> nodes, the loosest match.
        self._by_table: dict[str, list[DataNode]] = {}
        # node id -> lowercase column names, for candidate filtering.
        self._columns: dict[str, set[str]] = {}
        # node id -> column names in their real case, in schema order.
        self._column_names: dict[str, list[str]] = {}

        item_names = {i.id: i.display_name.lower() for i in self.items}
        # Exact addressing: item GUID, and (workspace, item name) for paths that
        # use names (`abfss://Sales@onelake…/lh_gold.Lakehouse/Tables/…`).
        self._item_by_guid: dict[str, Item] = {i.item_guid.lower(): i for i in self.items}
        workspaces = repository.list_workspaces(session, customer_id, tenant_id)
        self._ws_guid_by_name: dict[str, str] = {
            w.name.lower(): w.workspace_guid.lower() for w in workspaces
        }
        self._ws_guid_by_id: dict[str, str] = {
            w.id: w.workspace_guid.lower() for w in workspaces
        }
        # A lakehouse and its SQL endpoint share a name; the storage item wins,
        # because that is where the tables (and their nodes) live.
        self._item_by_ws_name: dict[tuple[str, str], Item] = {}
        for i in sorted(self.items, key=lambda i: i.item_type not in SQL_ENDPOINT_TYPES):
            key = (self._ws_guid_by_id.get(i.workspace_id, ""), i.display_name.lower())
            self._item_by_ws_name[key] = i
        # item db id -> {(schema, table): node} and {table: [nodes]}
        self._by_item: dict[str, dict[tuple[str, str], DataNode]] = {}
        self._by_item_table: dict[str, dict[str, list[DataNode]]] = {}
        for node in self.nodes:
            if node.item_id and node.table_name:
                schema = (node.schema_name or "").lower()
                table = node.table_name.lower()
                self._by_item.setdefault(node.item_id, {})[(schema, table)] = node
                self._by_item_table.setdefault(node.item_id, {}).setdefault(
                    table, []
                ).append(node)
        for node in self.nodes:
            lakehouse = item_names.get(node.item_id or "", "")
            schema = (node.schema_name or "").lower()
            table = (node.table_name or "").lower()
            if not table:
                continue
            self._by_full.setdefault((lakehouse, schema, table), node)
            self._by_schema_table.setdefault((schema, table), []).append(node)
            self._by_table.setdefault(table, []).append(node)
            # A report visual is named by its title but referenced in PBIR by
            # its id (`v-map-country`), so both have to resolve — otherwise the
            # graph stops at the semantic model and no report is ever connected.
            if node.kind == KIND_REPORT_VISUAL:
                visual_id = node.id.rsplit(":", 1)[-1].lower()
                if visual_id and visual_id != table:
                    self._by_table.setdefault(visual_id, []).append(node)

        columns = self._load_columns()
        for column in columns:
            self._column_names.setdefault(column.node_id, []).append(column.name)
        # Only a schema a collector read (Delta, OneLake, Scanner - they carry a
        # data type) is trusted to filter candidates. Columns created from code
        # are fragments: a shortcut table known only by the three columns some
        # notebook mentioned would otherwise veto every other real column.
        for column in columns:
            if column.data_type:
                self._columns.setdefault(column.node_id, set()).add(column.name.lower())

        self.unresolved: list[str] = []

    def _load_columns(self) -> list[ColumnNode]:
        from sqlalchemy import select

        stmt = select(ColumnNode).where(
            ColumnNode.customer_id == self.customer_id,
            ColumnNode.tenant_id == self.tenant_id,
        )
        return list(self.session.scalars(stmt))

    # --- table resolution -------------------------------------------------
    def resolve_table(
        self, ref: TableRef, *, workspace_guid: str = "", create_external: bool = True
    ) -> str | None:
        """
        A TableRef -> a node id, or None when it cannot be placed.

        Returns None rather than inventing a node: a phantom node in the graph is
        worse than a recorded gap, because it looks like real lineage.
        """
        if ref.dynamic_expr:
            return None

        if ref.is_external or ref.connection:
            return self._external_node(ref) if create_external else None

        if ref.path:
            return self._resolve_path(ref, workspace_guid)

        if ref.item_id:
            return self._resolve_in_item(
                self._item_by_guid.get(ref.item_id.lower()), ref.schema, ref.table, ref
            )

        if ref.workspace_id and ref.lakehouse:
            # `Sql.Database("<ws-encoded host>", "lh_gold")`: exact workspace,
            # item by name.
            item = self._item_by_ws_name.get(
                (ref.workspace_id.lower(), ref.lakehouse.lower())
            )
            if item is not None:
                return self._resolve_in_item(item, ref.schema, ref.table, ref)

        lakehouse = (ref.lakehouse or "").lower()
        schema = (ref.schema or "").lower()
        table = (ref.table or "").lower()
        if not table:
            return None

        exact = self._by_full.get((lakehouse, schema, table))
        if exact is not None:
            return exact.id

        # Named lakehouse we do not know, or none named: fall back on
        # schema.table, then table alone. Ambiguity is refused rather than
        # guessed — picking one of two candidates silently attaches lineage to
        # the wrong table.
        for candidates in (
            self._by_schema_table.get((schema, table)) if schema else None,
            self._by_table.get(table),
        ):
            if not candidates:
                continue
            if len(candidates) == 1:
                return candidates[0].id
            same_workspace = [
                c
                for c in candidates
                if workspace_guid and c.workspace_id == ids.workspace_id(workspace_guid)
            ]
            if len(same_workspace) == 1:
                return same_workspace[0].id
            self.unresolved.append(
                f"{ref.key()} matches {len(candidates)} tables — ambiguous."
            )
            return None

        self.unresolved.append(f"{ref.key()} is not a known table.")
        return None

    def _resolve_in_item(
        self, item: Item | None, schema: str, table: str, ref: TableRef
    ) -> str | None:
        """A table inside one known lakehouse / warehouse / mirrored database."""
        if item is not None and item.item_type in SQL_ENDPOINT_TYPES:
            # Direct Lake and SQL connections name the SQL endpoint; its tables
            # are the same-named lakehouse's (or mirrored database's).
            ws_guid = self._ws_guid_by_id.get(item.workspace_id, "")
            item = self._item_by_ws_name.get((ws_guid, item.display_name.lower()), item)
        if item is None:
            self.unresolved.append(
                f"{ref.key()}: item {ref.item_id or ref.lakehouse} is not in scope."
            )
            return None
        schema, table = (schema or "").lower(), (table or "").lower()
        exact = self._by_item.get(item.id, {}).get((schema, table))
        if exact is not None:
            return exact.id
        if item.item_type == "SemanticModel":
            # One node per model: any of its tables is the model.
            for node in self._by_item.get(item.id, {}).values():
                if node.kind == KIND_MODEL_TABLE:
                    return node.id
        if item.item_type == "Report":
            # Visuals are addressed by their PBIR id, which ends their node id.
            suffix = f":{table}"
            for node in self._by_item.get(item.id, {}).values():
                if node.id.lower().endswith(suffix):
                    return node.id
        if not schema or schema == "dbo":
            # A schema-less reference ("Tables/orders", `Id = "orders"`) names a
            # table in a lakehouse without schemas, where the table API reports
            # the synthetic `dbo` — or the one table of that name.
            candidates = self._by_item_table.get(item.id, {}).get(table) or []
            if len(candidates) == 1:
                return candidates[0].id
            # `orders` in a schema-enabled lakehouse holding both nav.orders and
            # dbo.orders: an unqualified name means the default schema, dbo.
            dbo = [c for c in candidates if (c.schema_name or "").lower() == "dbo"]
            if len(dbo) == 1:
                return dbo[0].id
        self.unresolved.append(
            f"{item.display_name} has no table {schema + '.' if schema else ''}{table}."
        )
        return None

    def register(self, node: DataNode) -> None:
        """Index a node created after the resolver was built (a report visual)."""
        if node.item_id and node.table_name:
            schema, table = (node.schema_name or "").lower(), node.table_name.lower()
            self._by_item.setdefault(node.item_id, {})[(schema, table)] = node
            self._by_item_table.setdefault(node.item_id, {}).setdefault(table, []).append(
                node
            )

    def _resolve_path(self, ref: TableRef, workspace_guid: str = "") -> str | None:
        """
        A OneLake path -> a node.

        Every form Fabric accepts, resolved to (workspace, item, schema, table):
          abfss://<ws name|guid>@onelake.dfs.fabric.microsoft.com/<item>/Tables/…
          https://onelake.dfs.fabric.microsoft.com/<ws>/<item>/Tables/…
          <item guid>/Tables/…  (workspace-relative)
          Tables/…  or  /lakehouse/default/Tables/…  (the default lakehouse)
        where <item> is a GUID or `Name.Lakehouse` / `Name.Warehouse`.
        """
        raw = ref.path.replace("\\", "/").strip()
        ws_part = item_part = ""
        rest = raw
        match = _ONELAKE_URL.match(raw)
        if match:
            ws_part = match.group("ws") or match.group("ws2")
            item_part, rest = match.group("item"), match.group("rest")
        else:
            parts = [p for p in raw.split("/") if p]
            if parts and parts[0].lower() == "lakehouse" and len(parts) > 1:
                parts = parts[2:]  # /lakehouse/default/Tables/...
            if parts and parts[0] != "Tables" and "Tables" in parts:
                item_part = parts[parts.index("Tables") - 1]
                parts = parts[parts.index("Tables") :]
            rest = "/".join(parts)

        segments = [p for p in rest.split("/") if p]
        if "Tables" not in segments:
            self.unresolved.append(f"path {ref.path} is not under Tables/.")
            return None
        tail = segments[segments.index("Tables") + 1 :]
        if not tail:
            return None
        table = tail[1] if len(tail) > 1 else tail[0]
        schema = tail[0] if len(tail) > 1 else ""

        item = None
        if item_part:
            ws_guid = ""
            if ws_part:
                ws_guid = (
                    ws_part.lower()
                    if _GUID.fullmatch(ws_part)
                    else self._ws_guid_by_name.get(ws_part.lower(), "")
                )
            ws_guid = ws_guid or workspace_guid.lower()
            if _GUID.fullmatch(item_part):
                item = self._item_by_guid.get(item_part.lower())
            else:
                name = item_part.rsplit(".", 1)[0] if "." in item_part else item_part
                item = self._item_by_ws_name.get((ws_guid, name.lower()))
        elif ref.item_id:
            item = self._item_by_guid.get(ref.item_id.lower())
        elif ref.lakehouse:
            item = self._item_by_ws_name.get(
                (workspace_guid.lower(), ref.lakehouse.lower())
            ) or next(
                (i for i in self.items if i.display_name.lower() == ref.lakehouse.lower()),
                None,
            )

        if item is not None:
            return self._resolve_in_item(item, schema, table, ref)

        # No item could be identified: fall back on the table name alone, and
        # only when it is unambiguous.
        candidates = self._by_schema_table.get((schema.lower(), table.lower())) or (
            self._by_table.get(table.lower())
        )
        if candidates and len(candidates) == 1:
            return candidates[0].id
        self.unresolved.append(f"path {ref.path} could not be resolved to one table.")
        return None

    def _external_node(self, ref: TableRef) -> str:
        """
        Find or create the external node for a source outside Fabric.

        Keyed on the connection fingerprint plus the object, so the same server
        seen by three pipelines is one connection with three tables.
        """
        node_id = ids.external_id(ref.connection, ref.table or "(unknown)", self.tenant_id)
        node = repository.get_node(self.session, self.customer_id, self.tenant_id, node_id)
        if node is None:
            detail = ref.connection or {}
            label = (
                detail.get("server")
                or detail.get("url")
                or detail.get("kind")
                or "External"
            )
            repository.upsert_node(
                self.session,
                self.customer_id,
                self.tenant_id,
                id=node_id,
                kind=KIND_EXTERNAL,
                layer=LAYER_SOURCE,
                layer_is_declared=True,
                schema_name="",
                table_name=ref.table or "(unknown)",
                display_path=f"External sources / {label} / {ref.table}",
                connection_hash=ids.connection_hash(detail),
                connection_detail=detail,
            )
            # Flush before anything references it: edges and columns carry FKs to
            # this node, and inside one transaction SQLite checks those at
            # statement time rather than at commit.
            self.session.flush()
        return node_id

    def code_source(self, item_id: str, *, unresolved: bool) -> str:
        """The placeholder source for a write with no nameable table source."""
        item = next((i for i in self.items if i.id == item_id), None)
        name = item.display_name if item else item_id
        label = f"Unresolved source in {name}" if unresolved else f"Generated in {name}"
        detail = {"kind": "unresolved" if unresolved else "code", "item": item_id}
        node_id = ids.external_id(detail, label, self.tenant_id)
        if (
            repository.get_node(self.session, self.customer_id, self.tenant_id, node_id)
            is None
        ):
            repository.upsert_node(
                self.session,
                self.customer_id,
                self.tenant_id,
                id=node_id,
                kind=KIND_EXTERNAL,
                layer=LAYER_SOURCE,
                layer_is_declared=True,
                schema_name="",
                table_name=label,
                display_path=f"{'Unresolved' if unresolved else 'Code'} / {label}",
                connection_hash=ids.connection_hash(detail),
                connection_detail=detail,
            )
            self.session.flush()
        return node_id

    # --- column resolution ------------------------------------------------
    def column_names(self, node_id: str) -> list[str]:
        return list(self._column_names.get(node_id, []))

    def schema_known(self, node_id: str) -> bool:
        """Whether a collector read this node's schema (not just code mentioning it)."""
        return node_id in self._columns

    def has_column(self, node_id: str, column: str) -> bool:
        known = self._columns.get(node_id)
        if known is None:
            # We have no schema for this node (an external source, or a table
            # the Delta collector has not reached). Accept the column rather
            # than dropping lineage for want of a schema we never had.
            return True
        return column.lower() in known

    def ensure_column(self, node_id: str, column: str, **fields) -> str:
        """
        Get or create a column row.

        Columns on external sources are only ever known from the code that reads
        them, so they are created here; lakehouse columns already exist from the
        Delta or OneLake collector and are left alone.
        """
        # Match an existing column case-insensitively and use ITS spelling: ids
        # are case-sensitive, and code often writes `consumer_no` for a Delta
        # column called `Consumer_No`.
        existing = next(
            (n for n in self._column_names.get(node_id, []) if n.lower() == column.lower()),
            None,
        )
        if existing is not None:
            return ids.column_id(node_id, existing)
        column_id = ids.column_id(node_id, column)
        repository.upsert_column(
            self.session,
            self.customer_id,
            self.tenant_id,
            id=column_id,
            node_id=node_id,
            name=column,
            **fields,
        )
        # Same reason as _external_node: a column_edge inserted in this
        # transaction has a FK to this row.
        self.session.flush()
        # Not added to _columns: a column seen in code is not a schema.
        self._column_names.setdefault(node_id, []).append(column)
        return column_id


def _dedupe_candidates(
    edges: list[ColumnEdgeRef],
) -> list[ColumnEdgeRef]:
    """
    Collapse candidate groups after filtering.

    When a join's candidates are narrowed to exactly one surviving source, that
    edge is no longer a guess — it is the only possibility consistent with the
    schema, so it is promoted out of low confidence.
    """
    by_target: dict[tuple[str, str], list[ColumnEdgeRef]] = {}
    for edge in edges:
        key = (edge.target.column, edge.source.column)
        by_target.setdefault(key, []).append(edge)

    out: list[ColumnEdgeRef] = []
    for group in by_target.values():
        if len(group) == 1 and group[0].confidence == CONF_LOW:
            only = group[0]
            out.append(
                ColumnEdgeRef(
                    source=only.source,
                    target=only.target,
                    transform=only.transform,
                    confidence=CONF_MEDIUM,
                )
            )
        else:
            out.extend(group)
    return out


def apply_parse_result(
    session: Session,
    resolver: Resolver,
    result: ParseResult,
    *,
    via_item_id: str,
    workspace_guid: str = "",
    source: str = SOURCE_PARSED,
) -> dict:
    """
    Write one parse result into the graph.

    Returns counts plus the references that could not be resolved, which become
    `collector_error` rows and the "unresolved lineage" finding.
    """
    stats = {"edges": 0, "columnEdges": 0, "externalNodes": 0, "unresolved": 0}
    before = len(resolver.unresolved)

    for write in result.writes:
        target_id = resolver.resolve_table(
            write.table, workspace_guid=workspace_guid, create_external=False
        )
        if target_id is None:
            if write.table.dynamic_expr:
                # A dynamic write target: record the fact against the item so the
                # UI can say lineage here is incomplete and why.
                stats["unresolved"] += 1
            continue

        made = 0
        read_ids: list[str] = []
        for read in write.reads:
            source_id = resolver.resolve_table(
                read, workspace_guid=workspace_guid, create_external=True
            )
            if source_id is None:
                continue
            if read.is_external or read.connection:
                stats["externalNodes"] += 1
            edge = repository.upsert_edge(
                session,
                resolver.customer_id,
                resolver.tenant_id,
                from_node_id=source_id,
                to_node_id=target_id,
                via_item_id=via_item_id,
                via_ref=write.via_ref,
                write_mode=write.mode,
                source=source,
                confidence=write.confidence,
                is_dynamic=False,
            )
            if edge is not None:
                stats["edges"] += 1
                made += 1
                read_ids.append(source_id)

        if made == 0:
            # The item certainly writes this table, even when its data comes from
            # no table we can name: an API call, literal rows, "Enter data", a
            # generated calendar — or a source we failed to resolve. Recording
            # the producer through an explicit placeholder keeps "who writes
            # this" answerable instead of showing the table as sourceless.
            placeholder = resolver.code_source(via_item_id, unresolved=bool(write.reads))
            repository.upsert_edge(
                session,
                resolver.customer_id,
                resolver.tenant_id,
                from_node_id=placeholder,
                to_node_id=target_id,
                via_item_id=via_item_id,
                via_ref=write.via_ref,
                write_mode=write.mode,
                source=source,
                confidence=CONF_LOW if write.reads else write.confidence,
                is_dynamic=False,
            )
            stats["edges"] += 1

        if not config.COLUMN_LINEAGE:
            continue
        if write.passthrough and read_ids:
            covered = {e.target.column.lower() for e in write.column_edges}
            stats["columnEdges"] += _pair_by_name(
                session,
                resolver,
                target_id,
                read_ids,
                covered,
                via_item_id=via_item_id,
                via_ref=write.via_ref,
                source=source,
            )

        stats["columnEdges"] += _apply_column_edges(
            session,
            resolver,
            write.column_edges,
            target_id=target_id,
            via_item_id=via_item_id,
            via_ref=write.via_ref,
            workspace_guid=workspace_guid,
            source=source,
        )

    # Column edges not attached to a write (TMDL measures, PBIR visuals).
    orphans = [e for e in result.column_edges if not _in_writes(e, result.writes)]
    if orphans and config.COLUMN_LINEAGE:
        stats["columnEdges"] += _apply_column_edges(
            session,
            resolver,
            orphans,
            target_id=None,
            via_item_id=via_item_id,
            via_ref="",
            workspace_guid=workspace_guid,
            source=source,
        )

    stats["unresolved"] += len(resolver.unresolved) - before
    return stats


def _pair_by_name(
    session: Session,
    resolver: Resolver,
    target_id: str,
    read_ids: list[str],
    covered: set[str],
    *,
    via_item_id: str,
    via_ref: str,
    source: str,
) -> int:
    """
    Column edges for a pass-through write: each target column from the
    same-named column of its source.

    The target's schema is known (Delta log / OneLake). A source with a known
    schema must actually have the column; one without (an external system seen
    only through code) is taken at its word, and its columns are created here —
    which is how an OData entity or a Dataverse table acquires columns at all.
    With several possible sources a column gets one low-confidence candidate per
    source, exactly like a join in SQL.
    """
    written = 0
    names = resolver.column_names(target_id)
    if resolver.schema_known(target_id):
        # Pair the real schema, not names older parses left behind.
        names = [n for n in names if resolver.has_column(target_id, n)]
    for column in names:
        if column.lower() in covered or column == "*" or column.endswith(".*"):
            continue
        reads = [r for r in dict.fromkeys(read_ids) if r != target_id]
        sources = _name_sources(resolver, reads, column)
        if not sources or len(sources) > MAX_NAME_CANDIDATES:
            # Too many equally plausible sources is not lineage, it is noise:
            # a dataflow reading twenty unschema'd tables would otherwise draw
            # `comment` from all twenty.
            continue
        confidence = CONF_MEDIUM if len(sources) == 1 else CONF_LOW
        for source_node in sources:
            from_column = resolver.ensure_column(source_node, column)
            row = repository.upsert_column_edge(
                session,
                resolver.customer_id,
                resolver.tenant_id,
                from_column_id=from_column,
                to_column_id=ids.column_id(target_id, column),
                via_item_id=via_item_id,
                via_ref=via_ref,
                transform_expr="",
                source=source,
                confidence=confidence,
            )
            if row is not None:
                written += 1
    return written


MAX_NAME_CANDIDATES = 3


def _name_sources(resolver: Resolver, reads: list[str], column: str) -> list[str]:
    """
    Which reads can supply `column` by name, strongest evidence first.

    1. A read whose collector-read schema has the column — definite; any such
       read wins outright.
    2. A read whose columns, as seen in code, include it.
    3. A read we know nothing about — only when it is the sole read, since
       then there is nowhere else the column could come from.
    """
    key = column.lower()
    typed = [
        r for r in reads if resolver.schema_known(r) and resolver.has_column(r, column)
    ]
    if typed:
        return typed
    unknown = [r for r in reads if not resolver.schema_known(r)]
    seen = [r for r in unknown if any(n.lower() == key for n in resolver.column_names(r))]
    if seen:
        return seen
    return unknown if len(reads) == 1 else []


def _in_writes(edge: ColumnEdgeRef, writes: list[Write]) -> bool:
    return any(edge in write.column_edges for write in writes)


def _apply_column_edges(
    session: Session,
    resolver: Resolver,
    edges: list[ColumnEdgeRef],
    *,
    target_id: str | None,
    via_item_id: str,
    via_ref: str,
    workspace_guid: str,
    source: str,
) -> int:
    """
    Resolve and write column edges, dropping candidates the schema rules out.

    This is where a join's guesses become facts: the parser emitted
    `LINEAMOUNT` as possibly coming from either salestable or salesline, and
    only one of them actually has that column.
    """
    resolved: list[ColumnEdgeRef] = []
    for edge in edges:
        source_node = resolver.resolve_table(
            edge.source.table, workspace_guid=workspace_guid, create_external=True
        )
        if source_node is None:
            continue
        if not resolver.has_column(source_node, edge.source.column):
            # The candidate is inconsistent with the real schema. Dropping it is
            # the whole point of emitting candidates in the first place.
            continue
        resolved.append(edge)

    written = 0
    for edge in _dedupe_candidates(resolved):
        source_node = resolver.resolve_table(
            edge.source.table, workspace_guid=workspace_guid, create_external=True
        )
        target_node = target_id or resolver.resolve_table(
            edge.target.table, workspace_guid=workspace_guid, create_external=False
        )
        if source_node is None or target_node is None:
            continue

        source_column = resolver.ensure_column(source_node, edge.source.column)
        target_column = resolver.ensure_column(target_node, edge.target.column)

        row = repository.upsert_column_edge(
            session,
            resolver.customer_id,
            resolver.tenant_id,
            from_column_id=source_column,
            to_column_id=target_column,
            via_item_id=via_item_id,
            via_ref=via_ref,
            transform_expr=edge.transform,
            source=source,
            confidence=edge.confidence,
        )
        if row is not None:
            written += 1
    return written
