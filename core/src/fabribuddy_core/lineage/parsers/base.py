"""
The parser contract.

Every parser is a pure function of (artifact text + context) -> ParseResult.
Pure matters for two reasons: a parser can be re-run over stored
`code_artifact` text without touching Microsoft, and it can be tested from a
fixture with no database.

A parser never raises for bad input. Customer code is full of things no parser
handles — a dynamically built table name, a dialect quirk, a cell that is half
written. Each becomes a `warning` and, where possible, a low-confidence or
`is_dynamic` edge that says what could not be resolved. Dropping it silently is
the one unacceptable outcome: the customer then believes the lineage is
complete.
"""

import base64
import binascii
import re
import uuid
from dataclasses import dataclass, field

CONF_LOW = "low"
CONF_MEDIUM = "medium"
CONF_HIGH = "high"


@dataclass(frozen=True)
class TableRef:
    """
    A table as the *code* names it, before resolution to a node id.

    Deliberately not a node id: `spark.read.table("customers")` names a table
    relative to the notebook's default lakehouse, and only the resolver knows
    what that is. Keeping the parsed form separate means the parsers stay pure
    and testable without a tenant.
    """

    lakehouse: str = ""
    schema: str = ""
    table: str = ""
    # A path-based reference (`abfss://…/Tables/foo`) rather than a named one.
    path: str = ""
    # An external system rather than a lakehouse table.
    is_external: bool = False
    connection: dict = field(default_factory=dict)
    # The unresolved expression, when the name was built at runtime.
    dynamic_expr: str = ""
    # GUIDs, when the code names the item by id rather than by name — Dataflow
    # Gen2 destinations and `Lakehouse.Contents` navigation always do. An id is
    # exact where a name can be ambiguous across workspaces, so the resolver
    # prefers it.
    workspace_id: str = ""
    item_id: str = ""

    @property
    def is_dynamic(self) -> bool:
        return bool(self.dynamic_expr)

    def key(self) -> str:
        """A comparison key, for deduplicating refs within one parse."""
        if self.path:
            return f"path:{self.path.lower()}"
        if self.dynamic_expr:
            return f"dyn:{self.dynamic_expr}"
        if self.item_id:
            return f"id:{self.item_id.lower()}.{self.schema.lower()}.{self.table.lower()}"
        return ".".join(
            p
            for p in (self.lakehouse.lower(), self.schema.lower(), self.table.lower())
            if p
        )


@dataclass(frozen=True)
class ColumnRef:
    table: TableRef
    column: str


@dataclass(frozen=True)
class ColumnEdgeRef:
    """One column-level derivation, with the expression that produced it."""

    source: ColumnRef
    target: ColumnRef
    transform: str = ""
    confidence: str = CONF_HIGH


@dataclass
class Write:
    """A table this artifact writes, and how."""

    table: TableRef
    mode: str = ""  # overwrite | append | merge | replace | create
    # Which cell / activity did it, so the Code tab can highlight the right part.
    via_ref: str = ""
    reads: list[TableRef] = field(default_factory=list)
    column_edges: list[ColumnEdgeRef] = field(default_factory=list)
    confidence: str = CONF_HIGH
    # The write copies its source's columns by name wherever it says nothing
    # else: a Copy with automatic mapping, `SELECT *`, `df.write` of an
    # unprojected frame. The resolver, which knows the target's real schema,
    # pairs each target column with the same-named source column.
    passthrough: bool = False


@dataclass
class ParseResult:
    reads: list[TableRef] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    column_edges: list[ColumnEdgeRef] = field(default_factory=list)
    # Other items this one invokes: %run, notebookutils.notebook.run, a pipeline
    # Notebook activity. Drives the orchestration view.
    invokes: list[str] = field(default_factory=list)
    # A pipeline's Notebook activities with the parameters they pass:
    # (notebook reference, {parameter: literal value}).
    invoke_parameters: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    # Tables the code *could* write but does not today: a dataflow query with
    # loading switched off, whose name is the table a default destination would
    # create. Never applied as lineage by itself; the resolver uses one only
    # when a table has no writer and its Delta log says this engine wrote it.
    dormant_writes: list[Write] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: str = CONF_HIGH

    def warn(self, message: str) -> None:
        # Bounded: a 2,000-cell generated notebook can otherwise produce a
        # warning list larger than the code it came from.
        if len(self.warnings) < 200 and message not in self.warnings:
            self.warnings.append(message)

    def merge(self, other: "ParseResult") -> None:
        self.reads.extend(other.reads)
        self.writes.extend(other.writes)
        self.column_edges.extend(other.column_edges)
        self.invokes.extend(other.invokes)
        self.invoke_parameters.extend(other.invoke_parameters)
        self.dormant_writes.extend(other.dormant_writes)
        for warning in other.warnings:
            self.warn(warning)
        if _RANK[other.confidence] < _RANK[self.confidence]:
            self.confidence = other.confidence


_RANK = {CONF_LOW: 0, CONF_MEDIUM: 1, CONF_HIGH: 2}


def parse_qualified(name: str) -> TableRef:
    """
    Split a Spark table name into its parts.

    Fabric is two-level inside a lakehouse (`schema.table`) and three-level
    across them (`lakehouse.schema.table`). A bare name is left unqualified for
    the resolver to place against the notebook's default lakehouse — guessing
    `dbo` here would silently attach tables to the wrong schema.
    """
    cleaned = name.strip().strip("`").replace("`", "")
    parts = [p for p in cleaned.split(".") if p]
    if len(parts) >= 3:
        return TableRef(lakehouse=parts[-3], schema=parts[-2], table=parts[-1])
    if len(parts) == 2:
        return TableRef(schema=parts[0], table=parts[1])
    if len(parts) == 1:
        return TableRef(table=parts[0])
    return TableRef()


# `<base32 tenant>-<base32 workspace>.datawarehouse.fabric.microsoft.com`: the
# SQL endpoint / warehouse host encodes the workspace GUID (little-endian bytes,
# base32, lowercase, unpadded). Decoding it identifies the workspace exactly,
# where the database name alone could match a lakehouse in any workspace.
_FABRIC_SQL_HOST = re.compile(
    r"(?P<tenant>[a-z2-7]{26})-(?P<workspace>[a-z2-7]{26})\.(?:datawarehouse|database)"
    r"\.fabric\.microsoft\.com",
    re.IGNORECASE,
)


def fabric_sql_workspace(host: str) -> str | None:
    """The workspace GUID a Fabric SQL host belongs to, or None if not one."""
    match = _FABRIC_SQL_HOST.search(host or "")
    if not match:
        return None
    label = match.group("workspace").upper()
    try:
        raw = base64.b32decode(label + "=" * (-len(label) % 8))[:16]
        return str(uuid.UUID(bytes_le=raw))
    except (binascii.Error, ValueError):
        return None


_SQL_DATABASE = re.compile(
    r'Sql\.Databases?\s*\(\s*"(?P<host>[^"]+)"\s*(?:,\s*"(?P<db>[^"]+)")?'
)
_SCHEMA_ITEM = re.compile(
    r'\[\s*Schema\s*=\s*"(?P<schema>[^"]+)"\s*,\s*Item\s*=\s*"(?P<item>[^"]+)"',
    re.IGNORECASE,
)
_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def fabric_sql_table(m_text: str) -> TableRef | None:
    """
    `Sql.Database("<fabric host>", "<lakehouse name | item id>"){[Schema, Item]}`
    -> a table in that workspace's lakehouse / warehouse. None for any other server.
    """
    call = _SQL_DATABASE.search(m_text or "")
    if not call:
        return None
    workspace = fabric_sql_workspace(call.group("host"))
    if workspace is None:
        return None
    nav = _SCHEMA_ITEM.search(m_text)
    if not nav:
        return None
    database = call.group("db") or ""
    if _GUID.match(database):
        return TableRef(
            workspace_id=workspace,
            item_id=database,
            schema=nav.group("schema"),
            table=nav.group("item"),
        )
    return TableRef(
        workspace_id=workspace,
        lakehouse=database,
        schema=nav.group("schema"),
        table=nav.group("item"),
    )
