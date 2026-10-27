"""
Deterministic node identifiers.

Two rules drive every choice here:

1. **Re-scans must upsert, not duplicate.** The id is derived only from things
   that do not change between scans — GUIDs and names — never from a scan
   timestamp or a row counter.
2. **An id has to survive a URL.** The obvious form `lh:{ws}/{lh}/{schema}.{t}`
   contains slashes, which means every route needs a `:path` converter and
   every fetch needs encoding. Colons separate the parts instead, so ids drop
   into a path segment unescaped.

GUIDs are lowercased on the way in: `/admin/groups` returns them upper-cased
and the Fabric REST APIs return them lower-cased, so the same workspace
otherwise produces two different ids depending on which collector saw it first.
"""

import hashlib
import json

PREFIX_WORKSPACE = "ws"
PREFIX_ITEM = "item"
PREFIX_LAKEHOUSE_TABLE = "lh"
PREFIX_WAREHOUSE_TABLE = "wh"
PREFIX_MIRRORED_TABLE = "mdb"
PREFIX_EXTERNAL = "ext"
PREFIX_MODEL_TABLE = "sm"
PREFIX_REPORT_VISUAL = "rpt"
PREFIX_PRINCIPAL = "prn"

# Separates a column from its table: `lh:ws:lh:sales.customers~email`.
#
# `~` and not `#`: a column id goes into a URL path (`/columns/{id}/trace`), and
# `#` starts a fragment — the browser would strip everything after it and the
# server would receive the table id with no column. `~` is unreserved in RFC
# 3986, so it needs no escaping.
COLUMN_SEP = "~"


def norm_id(value: str | None) -> str:
    """Lowercase and strip a GUID. See the module docstring for why."""
    return (value or "").strip().lower()


def _part(value: str | None) -> str:
    """
    Make one path component safe to embed.

    A colon inside a workspace or table name would split the id when it is
    parsed back, and a tilde would split a column id from its table, so both
    become underscores. Everything else is left alone: names are display text and
    mangling them would make ids unreadable.
    """
    return (value or "").strip().replace(":", "_").replace(COLUMN_SEP, "_")


def workspace_id(workspace_guid: str) -> str:
    return f"{PREFIX_WORKSPACE}:{norm_id(workspace_guid)}"


def item_id(workspace_guid: str, item_guid: str) -> str:
    return f"{PREFIX_ITEM}:{norm_id(workspace_guid)}:{norm_id(item_guid)}"


def lakehouse_table_id(
    workspace_guid: str, lakehouse_guid: str, schema: str, table: str
) -> str:
    """
    `lh:{wsGuid}:{lhGuid}:{schema}.{table}`

    Schema is part of the id even for non-schema-enabled lakehouses, where it
    is the synthetic `dbo` that the OneLake table API reports. Without it, a
    lakehouse that later gains schemas would renumber every one of its tables.
    """
    return (
        f"{PREFIX_LAKEHOUSE_TABLE}:{norm_id(workspace_guid)}:{norm_id(lakehouse_guid)}:"
        f"{_part(schema) or 'dbo'}.{_part(table)}"
    )


def warehouse_table_id(
    workspace_guid: str, warehouse_guid: str, schema: str, table: str
) -> str:
    return (
        f"{PREFIX_WAREHOUSE_TABLE}:{norm_id(workspace_guid)}:{norm_id(warehouse_guid)}:"
        f"{_part(schema) or 'dbo'}.{_part(table)}"
    )


def mirrored_table_id(
    workspace_guid: str, database_guid: str, schema: str, table: str
) -> str:
    return (
        f"{PREFIX_MIRRORED_TABLE}:{norm_id(workspace_guid)}:{norm_id(database_guid)}:"
        f"{_part(schema) or 'dbo'}.{_part(table)}"
    )


def semantic_model_id(workspace_guid: str, model_guid: str) -> str:
    """`sm:{ws}:{model}` — the one node that stands for a whole semantic model."""
    return f"{PREFIX_MODEL_TABLE}:{norm_id(workspace_guid)}:{norm_id(model_guid)}"


def model_table_id(workspace_guid: str, model_guid: str, table: str) -> str:
    return (
        f"{PREFIX_MODEL_TABLE}:{norm_id(workspace_guid)}:{norm_id(model_guid)}:"
        f"{_part(table)}"
    )


def report_id(workspace_guid: str, report_guid: str) -> str:
    """The one node that stands for a whole report in the Report lane."""
    return report_visual_id(workspace_guid, report_guid, "report")


def report_visual_id(workspace_guid: str, report_guid: str, visual_guid: str) -> str:
    return (
        f"{PREFIX_REPORT_VISUAL}:{norm_id(workspace_guid)}:{norm_id(report_guid)}:"
        f"{_part(visual_guid)}"
    )


# What actually identifies an external system. Everything else in a connection
# detail — the Fabric connection reference, timeouts, the source-type string —
# describes HOW it is reached, not WHAT it is, and hashing those splits one
# server into several nodes as soon as one pipeline names a connection and
# another does not.
IDENTITY_KEYS = ("server", "database", "url", "path", "site", "host")


def connection_hash(detail: dict) -> str:
    """
    A stable fingerprint for an external system.

    The same SQL Server reached by three different pipelines has to collapse to
    ONE external node, or the graph grows a duplicate source per consumer.

    Only the keys in IDENTITY_KEYS are hashed. Values are lowercased because
    connection strings vary in case between activities, and keys are sorted so
    dict ordering cannot change the result.

    The object name is deliberately NOT included — it is appended to the id
    separately, so one server with forty tables is one connection with forty
    nodes.
    """
    lowered = {str(k).strip().lower(): v for k, v in (detail or {}).items()}
    identifying = {
        key: str(lowered[key]).strip().lower()
        for key in IDENTITY_KEYS
        if lowered.get(key) not in (None, "")
    }
    if not identifying:
        # Nothing identifying at all: fall back to the whole detail rather than
        # collapsing every unidentifiable source into one node.
        identifying = {
            str(k): str(v).strip().lower()
            for k, v in sorted(lowered.items())
            if v not in (None, "")
        }
    blob = json.dumps(identifying, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def external_id(detail: dict, obj: str, tenant_id: str = "") -> str:
    """
    `ext:{hash}:{object}` — e.g. ext:9f2a…:dbo.SalesTable.

    Scoped to the connected tenant: two customers reading the same public API
    (or the same SQL Server name) must get two nodes. A Fabric item's GUID is
    unique per tenant; a URL is not, and an unscoped id let one tenant's scan
    update another tenant's row.
    """
    digest = connection_hash(detail)
    if tenant_id:
        digest = hashlib.sha256(f"{tenant_id}|{digest}".encode()).hexdigest()[:16]
    return f"{PREFIX_EXTERNAL}:{digest}:{_part(obj)}"


def principal_id(graph_id: str = "", upn: str = "", tenant_id: str = "") -> str:
    """
    Prefer the Graph object id; fall back to UPN.

    A group has no UPN and a guest may have no resolvable graphId, so neither
    alone is sufficient — but preferring graphId keeps a person stable across a
    rename, which is exactly when an access report must not fork.
    """
    key = norm_id(graph_id) or norm_id(upn)
    # Scoped to the connected tenant: a guest's e-mail, or a multi-tenant app's
    # id, is the same in every customer's tenant and must not share one row.
    if tenant_id:
        return f"{PREFIX_PRINCIPAL}:{tenant_id}:{key}"
    return f"{PREFIX_PRINCIPAL}:{key}"


def column_id(node_id: str, column_name: str) -> str:
    """
    `{nodeId}~{columnName}`

    Column names keep their original case: Delta and TMDL are case-preserving
    and the UI shows the real name. Matching is done case-insensitively by the
    callers that need it, not by flattening the identifier.
    """
    return f"{node_id}{COLUMN_SEP}{_part(column_name)}"


def split_column_id(column: str) -> tuple[str, str]:
    """Inverse of column_id. Returns (node_id, column_name)."""
    node, sep, name = column.partition(COLUMN_SEP)
    if not sep:
        raise ValueError(f"Not a column id: {column!r}")
    return node, name


def kind_of(node_id: str) -> str:
    """The prefix of an id, so a caller can tell a table from a visual."""
    return node_id.split(":", 1)[0] if ":" in node_id else ""
