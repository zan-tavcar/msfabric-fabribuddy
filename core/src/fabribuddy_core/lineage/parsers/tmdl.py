"""
Semantic model parser (TMDL, `definition/**/*.tmdl`).

TMDL is indentation-structured, not JSON, so this is a line-oriented reader of
the constructs that carry lineage:

  table <name>                     a model table
    column '<name>'                a column, with its sourceColumn
    measure '<name>' = <DAX>       a measure and what it references
    partition <name> = entity      Direct Lake: entityName / schemaName
    partition <name> = m           Import: an M expression (handed to the M parser)
  role <name>                      RLS, with its filter expressions

`mode: directLake` plus a fixed identity is what makes the indirect-access
finding possible, so both are extracted even though neither is lineage.
"""

import logging
import re

from .base import (
    CONF_HIGH,
    ParseResult,
    TableRef,
    Write,
    fabric_sql_table,
    fabric_sql_workspace,
)

log = logging.getLogger("lineage.parse.tmdl")

_QUOTED_NAME = re.compile(r"^(?P<kind>\w+)\s+(?P<name>'[^']*'|\"[^\"]*\"|[^\s=]+)")
_ASSIGN = re.compile(r"^(?P<key>[\w.]+)\s*:\s*(?P<value>.*)$")
# `measure 'Net Sales EUR' = SUM(Sales[Net Amount EUR])`
_MEASURE = re.compile(r"^measure\s+(?P<name>'[^']*'|\"[^\"]*\"|[^\s=]+)\s*=\s*(?P<dax>.*)$")
# Table[Column] references inside DAX.
_DAX_COLUMN = re.compile(r"(?:'(?P<tq>[^']+)'|(?P<t>\w+))\s*\[(?P<c>[^\]]+)\]")
# A bare [Measure] reference with no table.
_DAX_MEASURE = re.compile(r"(?<![\w\]'])\[(?P<m>[^\]]+)\]")


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


class ModelTable:
    def __init__(self, name: str):
        self.name = name
        self.columns: list[dict] = []
        self.measures: list[dict] = []
        self.partition_mode = ""
        self.entity_name = ""
        self.schema_name = ""
        self.expression_source = ""
        self.m_expression: list[str] = []


_EXPRESSION_HEAD = re.compile(r"^expression\s+(?P<name>'[^']+'|\S+)\s*=\s*(?P<rest>.*)$")
_STRING_PARAM = re.compile(r'^\s*"(?P<value>[^"]*)"\s*(?:meta\s*\[.*)?$', re.DOTALL)
_ONELAKE_URL = re.compile(
    r"https://onelake\.(?:dfs|blob)\.fabric\.microsoft\.com/(?P<ws>[0-9a-fA-F-]{36})/(?P<item>[0-9a-fA-F-]{36})",
    re.IGNORECASE,
)


def _parse_expressions(text: str) -> dict[str, str]:
    """
    `expression Name = <M>` blocks from expressions.tmdl, name -> M text.

    Two kinds matter: parameters (`expression Server = "host" meta [...]`),
    which partitions reference by name, and the Direct Lake connection
    (`expression DatabaseQuery = let database = Sql.Database(host, id) …`).
    """
    out: dict[str, str] = {}
    name = ""
    body: list[str] = []
    for raw in text.splitlines():
        head = _EXPRESSION_HEAD.match(raw) if not raw[:1].isspace() else None
        if head:
            if name:
                out[name] = "\n".join(body)
            name, body = _unquote(head.group("name")), [head.group("rest")]
            continue
        if raw and not raw[:1].isspace() and not raw.startswith("///"):
            if name:
                out[name] = "\n".join(body)
            name, body = "", []
            continue
        if name and not raw.strip().lower().startswith(
            ("lineagetag", "annotation", "querygroup")
        ):
            body.append(raw.strip())
    if name:
        out[name] = "\n".join(body)
    return out


def _with_parameters(m_text: str, expressions: dict[str, str]) -> str:
    """Inline string parameters and referenced expressions into a partition's M."""
    text = m_text
    for name, body in expressions.items():
        pattern = r'(?<![\w."])' + re.escape(name) + r'(?![\w"])|#"' + re.escape(name) + '"'
        if not re.search(pattern, text):
            continue
        literal = _STRING_PARAM.match(body)
        if literal:
            text = re.sub(
                pattern, '"' + literal.group("value").replace("\\", "\\\\") + '"', text
            )
        else:
            text = f"{text}\n{body}"
    return text


def _import_source(m_text: str, expressions: dict[str, str]) -> TableRef | None:
    """
    The table an Import partition reads.

    Fabric sources first — a lakehouse or warehouse through its SQL endpoint
    (`Sql.Database("<ws-encoded host>", "lh")`) or `Lakehouse.Contents` — then
    any external system the mashup parser knows.
    """
    from .mashup import _fabric_tables, _Query, _source_from

    text = _with_parameters(m_text, expressions)
    fabric = fabric_sql_table(text)
    if fabric is not None:
        return fabric
    if "Lakehouse.Contents" in text or "Fabric.Warehouse" in text:
        refs = _fabric_tables(_Query("partition", "", text), {})
        if refs:
            return refs[0]
    source, _function = _source_from(text)
    return source


def _direct_lake_source(table, expressions: dict[str, str]) -> TableRef:
    """
    A Direct Lake table's source, placed in its lakehouse when the model says which.

    The partition names only the entity; the lakehouse comes from the
    expression it points at (`expressionSource: DatabaseQuery`) — a SQL
    endpoint (`Sql.Database(host, "<endpoint id>")`) or, for Direct Lake on
    OneLake, `AzureStorage.DataLake("https://onelake…/<ws>/<item>")`.
    """
    schema = table.schema_name or "dbo"
    body = expressions.get(table.expression_source) or (
        next(iter(expressions.values()), "") if len(expressions) == 1 else ""
    )
    onelake = _ONELAKE_URL.search(body)
    if onelake:
        return TableRef(
            workspace_id=onelake.group("ws"),
            item_id=onelake.group("item"),
            schema=schema,
            table=table.entity_name,
        )
    call = re.search(r'Sql\.Database\s*\(\s*"(?P<host>[^"]+)"\s*,\s*"(?P<db>[^"]+)"', body)
    if call:
        workspace = fabric_sql_workspace(call.group("host"))
        if workspace:
            database = call.group("db")
            if re.fullmatch(r"[0-9a-fA-F-]{36}", database):
                return TableRef(
                    workspace_id=workspace,
                    item_id=database,
                    schema=schema,
                    table=table.entity_name,
                )
            return TableRef(
                workspace_id=workspace,
                lakehouse=database,
                schema=schema,
                table=table.entity_name,
            )
    return TableRef(schema=schema, table=table.entity_name)


def parse_tmdl(
    documents: dict[str, str], *, model_item_id: str = ""
) -> tuple[ParseResult, dict]:
    """
    Parse every TMDL part of one semantic model.

    `documents` maps part path -> text, as stored in `code_artifact`. Returns
    (ParseResult, model info) where model info carries storage mode, fixed
    identity and RLS roles — needed by the access resolver, not by lineage.
    """
    result = ParseResult()
    tables: list[ModelTable] = []
    roles: list[dict] = []
    model_info = {
        "storage_mode": "",
        "uses_fixed_identity": False,
        "rls_roles": roles,
        "expressions": {},
    }

    expressions: dict[str, str] = {}
    for path, text in sorted(documents.items()):
        if not text:
            continue
        lowered = path.lower()
        if "expressions" in lowered or text.lstrip().startswith("expression "):
            expressions.update(_parse_expressions(text))
        if (
            "/tables/" in lowered
            or lowered.endswith("table.tmdl")
            or "table " in text[:200]
        ):
            tables.extend(_parse_tables(text, result))
        if "/roles/" in lowered or text.lstrip().startswith("role "):
            roles.extend(_parse_roles(text))
        if (
            "expressions" in lowered
            or "/model" in lowered
            or lowered.endswith("model.tmdl")
        ):
            _parse_model(text, model_info)

    # A model whose parts were not split by folder: parse the whole thing.
    if not tables:
        for text in documents.values():
            tables.extend(_parse_tables(text, result))

    # Model-level lineage: one write into the model node, reading every source
    # any of its tables loads from. Which table comes from where is kept in
    # model_info for the model page, not as edges.
    model_target = (
        TableRef(item_id=model_item_id, table="model")
        if model_item_id
        else TableRef(table="model")
    )
    sources: dict[str, TableRef] = {}
    table_sources: dict[str, TableRef | None] = {}

    for table in tables:
        source: TableRef | None = None
        if table.partition_mode == "directlake" and table.entity_name:
            source = _direct_lake_source(table, expressions)
            if not model_info["storage_mode"]:
                model_info["storage_mode"] = "DirectLake"
        elif table.m_expression:
            source = _import_source("\n".join(table.m_expression), expressions)
            if source is None and not table.measures:
                result.warn(f"table {table.name}: import partition source not recognised.")
            if not model_info["storage_mode"]:
                model_info["storage_mode"] = "Import"
        elif table.partition_mode:
            model_info["storage_mode"] = model_info["storage_mode"] or (
                "DirectQuery" if "directquery" in table.partition_mode else ""
            )
        table_sources[table.name] = source
        if source is not None:
            key = source.key() + str(sorted((source.connection or {}).items()))
            sources.setdefault(key, source)

    if sources:
        result.writes.append(
            Write(
                table=model_target,
                mode=(model_info["storage_mode"] or "model").lower(),
                via_ref=f"{len(tables)} tables",
                reads=list(sources.values()),
                confidence=CONF_HIGH,
            )
        )

    model_info["tables"] = [
        {
            "name": t.name,
            "columns": t.columns,
            "measures": [{"name": m["name"], "dax": m["dax"]} for m in t.measures],
            "mode": t.partition_mode,
            "entity_name": t.entity_name,
            "schema_name": t.schema_name,
            "source": table_sources.get(t.name),
        }
        for t in tables
    ]
    return result, model_info


def _parse_tables(text: str, result: ParseResult) -> list[ModelTable]:
    tables: list[ModelTable] = []
    current: ModelTable | None = None
    current_column: dict | None = None
    in_partition = False
    partition_indent = 0
    collecting_m = False

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("///"):
            continue
        indent = _indent(raw)
        line = raw.strip()

        header = _QUOTED_NAME.match(line)
        kind = header.group("kind").lower() if header else ""

        if kind == "table" and "=" not in line.split(None, 2)[0]:
            current = ModelTable(_unquote(header.group("name")))
            tables.append(current)
            current_column = None
            in_partition = collecting_m = False
            continue

        if current is None:
            continue

        measure = _MEASURE.match(line)
        if measure:
            dax = measure.group("dax").strip()
            current.measures.append(
                {
                    "name": _unquote(measure.group("name")),
                    "dax": dax,
                    "references": _dax_references(dax),
                }
            )
            current_column = None
            in_partition = collecting_m = False
            continue

        if kind == "column":
            current_column = {
                "name": _unquote(header.group("name")),
                "data_type": "",
                "source_column": "",
                "description": "",
            }
            current.columns.append(current_column)
            in_partition = collecting_m = False
            continue

        if kind == "partition":
            in_partition = True
            partition_indent = indent
            collecting_m = False
            current_column = None
            # `partition Sales = entity` / `= m` / `= calculated`
            _, _, tail = line.partition("=")
            current.partition_mode = tail.strip().lower() or current.partition_mode
            continue

        if in_partition and indent <= partition_indent and kind not in ("source", "mode"):
            in_partition = collecting_m = False

        assignment = _ASSIGN.match(line)
        if assignment:
            key = assignment.group("key").lower()
            value = assignment.group("value").strip()
            if current_column is not None:
                if key == "datatype":
                    current_column["data_type"] = value
                elif key == "sourcecolumn":
                    current_column["source_column"] = _unquote(value)
                elif key == "description":
                    current_column["description"] = _unquote(value)
            if in_partition:
                if key == "mode":
                    current.partition_mode = value.lower()
                elif key == "entityname":
                    current.entity_name = _unquote(value)
                elif key == "schemaname":
                    current.schema_name = _unquote(value)
                elif key == "expressionsource":
                    current.expression_source = _unquote(value)
            continue

        if in_partition and (collecting_m or line.startswith("let") or "=" in line):
            collecting_m = True
            current.m_expression.append(line)

    # A Direct Lake column with no explicit sourceColumn maps by name — that is
    # TMDL's default, not a gap, so filling it in here keeps the resolver simple.
    for table in tables:
        if table.partition_mode == "directlake":
            for column in table.columns:
                if not column["source_column"]:
                    column["source_column"] = column["name"]
    return tables


def _parse_roles(text: str) -> list[dict]:
    """RLS roles and their filter expressions."""
    roles: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("role "):
            current = {"name": _unquote(line[5:].split("=")[0]), "filters": []}
            roles.append(current)
            continue
        if current is None:
            continue
        if line.lower().startswith("tablepermission "):
            _, _, tail = line.partition(" ")
            name, _, expression = tail.partition("=")
            current["filters"].append(
                {"table": _unquote(name), "expression": expression.strip()}
            )
    return roles


def _parse_model(text: str, model_info: dict) -> None:
    """Model-level facts: default mode, and whether a fixed identity is used."""
    lowered = text.lower()
    if "directlake" in lowered and not model_info["storage_mode"]:
        model_info["storage_mode"] = "DirectLake"
    # Fabric records a fixed-identity Direct Lake connection either as an
    # explicit effective identity or as the absence of SSO on the expression.
    if "fixedidentity" in lowered.replace(" ", "") or "effectiveidentity" in lowered:
        model_info["uses_fixed_identity"] = True
    if "directlakebehavior" in lowered.replace(" ", ""):
        model_info["storage_mode"] = model_info["storage_mode"] or "DirectLake"


def _dax_references(dax: str) -> list[dict]:
    """
    `Table[Column]` and bare `[Measure]` references inside a DAX expression.

    A tokeniser rather than a DAX parser: the goal is "which fields does this
    measure touch", which regex answers well enough, and a wrong extra reference
    is visible in the UI rather than silently wrong.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for match in _DAX_COLUMN.finditer(dax):
        table = match.group("tq") or match.group("t") or ""
        column = match.group("c")
        key = (table.lower(), column.lower())
        if key not in seen:
            seen.add(key)
            out.append({"table": table, "column": column})
    # Strip the Table[Column] hits before looking for bare measures, or every
    # column reference would also register as a measure.
    without_columns = _DAX_COLUMN.sub(" ", dax)
    for match in _DAX_MEASURE.finditer(without_columns):
        name = match.group("m")
        key = ("", name.lower())
        if key not in seen:
            seen.add(key)
            out.append({"table": "", "column": name})
    return out
