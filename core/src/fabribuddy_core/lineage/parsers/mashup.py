"""
Dataflow Gen2 parser (`mashup.pq` + `queryMetadata.json`).

M is a real language and this is not a real M parser — it is a regex-based
reader of the step shapes that appear in practice. That is a deliberate
trade-off, and the honest consequence is that unrecognised steps are *reported*
rather than ignored: a dataflow whose steps we could not read shows as
"3 steps not understood" instead of silently claiming clean lineage.

Recognised sources: Sql.Database, SharePoint.Files, Excel.Workbook,
Lakehouse.Contents, Web.Contents, Csv.Document, Odbc.DataSource.
Recognised column steps: Table.RenameColumns, Table.SelectColumns,
Table.AddColumn, Table.TransformColumnTypes.
"""

import json
import logging
import re

from .base import (
    CONF_LOW,
    CONF_MEDIUM,
    ColumnEdgeRef,
    ColumnRef,
    ParseResult,
    TableRef,
    Write,
    fabric_sql_table,
)

log = logging.getLogger("lineage.parse.mashup")

# `shared Query = let … in …;` or a bare `let … in …` per query.
_QUERY = re.compile(
    r"(?:shared\s+)?(?P<name>[A-Za-z_][\w.]*|#\"[^\"]+\")\s*=\s*let\b(?P<body>.*?)\bin\b\s*(?P<result>[^;]+);",
    re.DOTALL,
)
_STEP = re.compile(
    r"(?P<name>#?\"[^\"]+\"|[A-Za-z_]\w*)\s*=\s*(?P<expr>.*?)(?=,\s*(?:#?\"[^\"]+\"|[A-Za-z_]\w*)\s*=|$)",
    re.DOTALL,
)

_SOURCES = {
    "Sql.Database": "sql",
    "Sql.Databases": "sql",
    "SharePoint.Files": "sharepoint",
    "SharePoint.Contents": "sharepoint",
    "Excel.Workbook": "excel",
    "Lakehouse.Contents": "lakehouse",
    "Web.Contents": "web",
    "Csv.Document": "csv",
    "Odbc.DataSource": "odbc",
    "AzureStorage.DataLake": "adls",
    "AzureStorage.Blobs": "blob",
}

_RENAME = re.compile(r"Table\.RenameColumns\s*\(", re.IGNORECASE)
_PAIR = re.compile(r"\{\s*\"(?P<old>[^\"]+)\"\s*,\s*\"(?P<new>[^\"]+)\"\s*\}")
_SELECT = re.compile(
    r"Table\.SelectColumns\s*\([^,]+,\s*\{(?P<cols>[^}]*)\}", re.IGNORECASE
)
_ADD = re.compile(
    r"Table\.AddColumn\s*\([^,]+,\s*\"(?P<name>[^\"]+)\"\s*,\s*(?P<expr>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED = re.compile(r"\"([^\"]+)\"")
_FILE_NAME = re.compile(r"\[\s*Name\s*=\s*\"(?P<file>[^\"]+)\"", re.IGNORECASE)
_NAVIGATION = re.compile(r"\{\s*\[\s*(?:Name|Item)\s*=|Excel\.Workbook\s*\(")
_ITEM_NAME = re.compile(r"\[\s*Item\s*=\s*\"(?P<item>[^\"]+)\"", re.IGNORECASE)


def _clean_name(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('#"') and raw.endswith('"'):
        return raw[2:-1]
    return raw.strip('"')


def _strip_comments(text: str) -> str:
    """
    Remove M comments without touching string contents.

    A naive `//` strip destroys every URL in the document — `SharePoint.Files(
    "https://…")` loses everything after the scheme, and the connection
    fingerprint then identifies the wrong system. So this walks the text and
    only treats `//` and `/*` as comments when outside a quoted string.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == '"':
                # M escapes a quote by doubling it.
                if i + 1 < n and text[i + 1] == '"':
                    out.append(text[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                out.append(" ")
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _source_from(expression: str, context: str = "") -> tuple[TableRef | None, str]:
    """
    (external source, the function name that produced it).

    `context` is the whole query body. The object being read is usually named in
    a LATER step than the connection — `Source = SharePoint.Files(site)` then
    `File = Source{[Name = "fx_rates.xlsx"]}` — so without it the node would be
    named after the SharePoint site and every file on that site would collapse
    into one source.
    """
    for function, kind in _SOURCES.items():
        if function not in expression:
            continue
        arguments = _QUOTED.findall(expression)
        detail: dict = {"kind": kind}
        if kind == "sql" and arguments:
            detail["server"] = arguments[0]
            if len(arguments) > 1:
                detail["database"] = arguments[1]
        elif arguments:
            detail["url"] = arguments[0]

        # The object is the file or sheet where one is named, else the host.
        haystack = f"{expression}\n{context}"
        file_match = _FILE_NAME.search(haystack)
        item_match = _ITEM_NAME.search(haystack)
        obj = (
            (file_match.group("file") if file_match else "")
            or (item_match.group("item") if item_match else "")
            or (arguments[1] if kind == "sql" and len(arguments) > 1 else "")
            or (arguments[0] if arguments else kind)
        )
        return (
            TableRef(table=obj, is_external=kind != "lakehouse", connection=detail),
            function,
        )
    return None, ""


# --- section document ------------------------------------------------------
# A Gen2 `mashup.pq` is a Power Query *section document*: optional section-level
# attributes, `section Section1;`, then `[attributes] shared Name = expression;`
# members. Destinations live in those attributes (see Microsoft Learn, "Advanced
# edit for data destination queries in Dataflow Gen2" and "Dataflow Gen2 default
# destination"):
#
#   [DataDestinations = {[Definition = [Kind = "Reference",
#        QueryName = "orders_DataDestination", IsNewTarget = true],
#        Settings = [..., UpdateMethod = [Kind = "Replace"], ...]]}]
#   shared orders = let ... ;
#   shared orders_DataDestination = let
#       Pattern = Lakehouse.Contents([...]),
#       Navigation_1 = Pattern{[workspaceId = "…"]}[Data],
#       Navigation_2 = Navigation_1{[lakehouseId = "…"]}[Data],
#       TableNavigation = Navigation_2{[Id = "orders", ItemKind = "Table"]}?[Data]?
#   in TableNavigation;
#
# or, with a default destination, one section-level attribute
#   [DefaultOutputDestinationSettings = [DestinationDefinition = [Kind =
#        "Reference", QueryName = "DefaultDestination", ...], UpdateMethod = ...]]
# plus `[BindToDefaultDestination = true]` on each query, which then writes a
# table named after the query into the default destination's lakehouse.

_ATTR_QUERY_NAME = re.compile(r'QueryName\s*=\s*"(?P<name>[^"]+)"')
_UPDATE_METHOD = re.compile(r'UpdateMethod\s*=\s*\[\s*Kind\s*=\s*"(?P<kind>\w+)"')
_BIND_DEFAULT = re.compile(r"BindToDefaultDestination\s*=\s*true", re.IGNORECASE)
_DEFAULT_SETTINGS = "DefaultOutputDestinationSettings"
_VARIABLE = re.compile(
    r'Variable\.(?:ValueOrDefault|Value)\s*\(\s*"(?P<ref>[^"]+)"\s*(?:,\s*(?P<default>"(?:[^"]|"")*"|[^)]+?))?\s*\)'
)
_VAR_PATH = re.compile(r"\$\(\s*/\*\*/(?P<lib>[^/)]+)/(?P<var>[^)]+)\)")
_STRING = re.compile(r'^"((?:[^"]|"")*)"$')
_NAV_TOKEN = re.compile(
    r'(?P<key>workspaceId|lakehouseId|warehouseId|databaseId|itemId)\s*=\s*(?P<val>"[^"]*"|[A-Za-z_][\w.]*|#"[^"]+")'
    r'|\[\s*Id\s*=\s*"(?P<id>[^"]+)"\s*,\s*ItemKind\s*=\s*"(?P<kind>\w+)"'
    r'|\[\s*ItemKind\s*=\s*"(?P<kind2>\w+)"\s*,\s*Id\s*=\s*"(?P<id2>[^"]+)"'
    r'|\[\s*Schema\s*=\s*"(?P<schema>[^"]+)"\s*,\s*Item\s*=\s*"(?P<item>[^"]+)"',
    re.IGNORECASE,
)
_FABRIC_CONNECTORS = ("Lakehouse.Contents", "Fabric.Warehouse", "Warehouse.Contents")
_FABRIC_SQL_HOST = re.compile(r"\.(?:datawarehouse|database)\.fabric\.microsoft\.com", re.I)


def _split_members(text: str) -> list[str]:
    """Split a section document on top-level `;`, respecting strings and brackets."""
    out, buf, depth, in_string, i = [], [], 0, False, 0
    while i < len(text):
        ch = text[i]
        buf.append(ch)
        if in_string:
            if ch == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            out.append("".join(buf[:-1]))
            buf = []
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def _leading_attributes(chunk: str) -> tuple[str, str]:
    """Peel `[ ... ]` attribute records off the front of a member."""
    attrs, rest = [], chunk.lstrip()
    while rest.startswith("["):
        depth, in_string = 0, False
        for i, ch in enumerate(rest):
            if in_string:
                in_string = ch != '"'
                continue
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    attrs.append(rest[: i + 1])
                    rest = rest[i + 1 :].lstrip()
                    break
        else:
            break
    return " ".join(attrs), rest


_MEMBER = re.compile(
    r'^shared\s+(?P<name>#"[^"]+"|[A-Za-z_][\w.]*)\s*=\s*(?P<expr>.*)$', re.DOTALL
)


class _Query:
    __slots__ = ("name", "attrs", "expr", "steps", "consts")

    def __init__(self, name: str, attrs: str, expr: str):
        self.name, self.attrs, self.expr = name, attrs, expr
        body = expr
        let = re.match(r"^\s*let\b(?P<body>.*)\bin\b\s*(?P<result>.*)$", expr, re.DOTALL)
        if let:
            body = let.group("body")
        self.steps = (
            [
                (_clean_name(m.group("name")), m.group("expr").strip())
                for m in _STEP.finditer(body)
            ]
            if let
            else [("", expr.strip())]
        )
        self.consts: dict[str, str] = {}


def _string_value(expression: str, variables: dict[str, str]) -> str | None:
    """A literal string, or a Variable Library reference resolved to its value."""
    expression = re.sub(r"\s+meta\s+\[.*$", "", expression.strip(), flags=re.DOTALL).strip()
    literal = _STRING.match(expression)
    if literal:
        return literal.group(1).replace('""', '"')
    var = _VARIABLE.fullmatch(expression)
    if var:
        path = _VAR_PATH.search(var.group("ref"))
        if path:
            key = f"{path.group('lib').strip()}/{path.group('var').strip()}".lower()
            if key in variables:
                return str(variables[key])
        default = (var.group("default") or "").strip()
        literal = _STRING.match(default)
        if literal:
            return literal.group(1).replace('""', '"')
    return None


def _fabric_tables(query: _Query, globals_: dict[str, str]) -> list[TableRef]:
    """
    Lakehouse / warehouse tables a query navigates to, by workspace and item id.

    Tokens are read in document order, so each `{[Id = "t", ItemKind =
    "Table"]}` is paired with the workspace and item navigated to just before
    it — a query that joins two lakehouse tables yields two refs.
    """
    # The raw expression, not the split steps: the step splitter breaks a
    # record like `[Id = "t", ItemKind = "Table"]` at its inner comma.
    text = query.expr
    if not any(c in text for c in _FABRIC_CONNECTORS):
        return []
    refs: list[TableRef] = []
    ws = item = schema = ""
    for m in _NAV_TOKEN.finditer(text):
        if m.group("key"):
            raw = m.group("val")
            value = (
                _STRING.match(raw).group(1)
                if raw.startswith('"')
                else query.consts.get(_clean_name(raw))
                or globals_.get(_clean_name(raw), "")
            )
            if m.group("key").lower() == "workspaceid":
                ws, item, schema = value, "", ""
            else:
                item, schema = value, ""
            continue
        ident = m.group("id") or m.group("id2")
        kind = (m.group("kind") or m.group("kind2") or "").lower()
        if ident and kind == "schema":
            schema = ident
            continue
        if ident and kind == "table":
            table_schema, table = schema, ident
            if not table_schema and "." in ident:
                table_schema, _, table = ident.partition(".")
            refs.append(
                TableRef(workspace_id=ws, item_id=item, schema=table_schema, table=table)
            )
            continue
        if m.group("item"):
            refs.append(
                TableRef(
                    workspace_id=ws,
                    item_id=item,
                    schema=m.group("schema"),
                    table=m.group("item"),
                )
            )
    return [r for r in refs if r.item_id or r.table]


def _fabric_container(query: _Query, globals_: dict[str, str]) -> TableRef | None:
    """The lakehouse/warehouse a default destination points at (no table)."""
    # The raw expression, not the split steps: the step splitter breaks a
    # record like `[Id = "t", ItemKind = "Table"]` at its inner comma.
    text = query.expr
    ws = item = schema = ""
    for m in _NAV_TOKEN.finditer(text):
        ident = m.group("id") or m.group("id2")
        if ident and (m.group("kind") or m.group("kind2") or "").lower() == "schema":
            # A default destination on a schema-enabled lakehouse navigates into
            # a schema; its tables land there, not wherever a name matches.
            schema = ident
            continue
        if not m.group("key"):
            continue
        raw = m.group("val")
        value = (
            _STRING.match(raw).group(1)
            if raw.startswith('"')
            else query.consts.get(_clean_name(raw)) or globals_.get(_clean_name(raw), "")
        )
        if m.group("key").lower() == "workspaceid":
            ws = value
        else:
            item = value
    return TableRef(workspace_id=ws, item_id=item, schema=schema) if item else None


def _fabric_sql_endpoint(expression: str, context: str) -> TableRef | None:
    """`Sql.Database("x.datawarehouse.fabric.microsoft.com", "lh_gold")` is a Fabric item, not an external server."""
    if "Sql.Database" not in expression or not _FABRIC_SQL_HOST.search(expression):
        return None
    arguments = _QUOTED.findall(expression)
    if len(arguments) < 2:
        return None
    nav = re.search(
        r'\[\s*Schema\s*=\s*"(?P<s>[^"]+)"\s*,\s*Item\s*=\s*"(?P<t>[^"]+)"', context
    )
    if not nav:
        return None
    return TableRef(lakehouse=arguments[1], schema=nav.group("s"), table=nav.group("t"))


def parse_mashup(
    mashup: str,
    query_metadata: dict | None = None,
    *,
    variables: dict[str, str] | None = None,
) -> ParseResult:
    """
    Parse a Dataflow Gen2 definition.

    `variables` maps `library/variable` (lowercase) to the value in the
    workspace's active value set, so `Variable.ValueOrDefault("$(/**/lib/var)",
    "…")` resolves to what actually runs rather than to the design-time default.
    """
    result = ParseResult()
    if not mashup or not mashup.strip():
        result.warn("Empty mashup document.")
        result.confidence = CONF_LOW
        return result

    variables = {k.lower(): v for k, v in (variables or {}).items()}
    text = _strip_comments(mashup)

    section_attrs = ""
    queries: dict[str, _Query] = {}
    for chunk in _split_members(text):
        attrs, rest = _leading_attributes(chunk)
        if rest.startswith("section"):
            section_attrs = attrs
            continue
        member = _MEMBER.match(rest)
        if not member:
            bare = _QUERY.search(chunk + ";")
            if bare:  # a lone `Name = let … in …` outside a section
                member_name = _clean_name(bare.group("name"))
                queries[member_name] = _Query(
                    member_name,
                    attrs,
                    f"let {bare.group('body')} in {bare.group('result')}",
                )
            continue
        name = _clean_name(member.group("name"))
        queries[name] = _Query(name, attrs, member.group("expr").strip())

    if not queries:
        result.warn("No M queries recognised in the mashup document.")
        result.confidence = CONF_LOW
        return result

    # Constants: parameter queries and literal/variable steps.
    globals_: dict[str, str] = {}
    for query in queries.values():
        value = _string_value(query.expr, variables) if len(query.steps) == 1 else None
        if value is not None:
            globals_[query.name] = value
        for step_name, expression in query.steps:
            value = _string_value(expression, variables)
            if value is not None and step_name:
                query.consts[step_name] = value

    # Which queries are destinations (never sources), and who writes where.
    default_match = (
        _ATTR_QUERY_NAME.search(section_attrs)
        if _DEFAULT_SETTINGS in section_attrs
        else None
    )
    default_name = default_match.group("name") if default_match else ""
    default_mode = _UPDATE_METHOD.search(section_attrs) if default_name else None
    destination_names = {default_name} if default_name else set()
    targets: dict[str, tuple[TableRef, str]] = {}
    for query in queries.values():
        if "DataDestinations" in query.attrs:
            dest = _ATTR_QUERY_NAME.search(query.attrs)
            mode = _UPDATE_METHOD.search(query.attrs)
            if dest and dest.group("name") in queries:
                destination_names.add(dest.group("name"))
                navs = _fabric_tables(queries[dest.group("name")], globals_)
                if navs:
                    targets[query.name] = (
                        navs[-1],
                        (mode.group("kind") if mode else "Replace"),
                    )
        elif _BIND_DEFAULT.search(query.attrs) and default_name in queries:
            container = _fabric_container(queries[default_name], globals_)
            if container is not None:
                targets[query.name] = (
                    TableRef(
                        workspace_id=container.workspace_id,
                        item_id=container.item_id,
                        schema=container.schema,
                        table=destination_table_name(query.name),
                    ),
                    default_mode.group("kind") if default_mode else "Replace",
                )
    legacy = _destinations(query_metadata or {})
    loaded = _load_enabled(query_metadata or {})

    # Where this dataflow lands tables: its default destination and every
    # explicit destination's lakehouse. A query that loads nowhere today may
    # have loaded into one of these in an earlier version of the dataflow.
    containers: dict[str, TableRef] = {}
    if default_name in queries:
        container = _fabric_container(queries[default_name], globals_)
        if container is not None:
            containers[container.key()] = container
    for target, _mode in targets.values():
        if target.item_id:
            container = TableRef(
                workspace_id=target.workspace_id,
                item_id=target.item_id,
                schema=target.schema,
            )
            containers.setdefault(container.key(), container)

    # Per query: direct sources, and references to other queries.
    direct: dict[str, list[TableRef]] = {}
    references: dict[str, list[str]] = {}
    column_steps: dict[str, tuple] = {}
    unparsed_by: dict[str, int] = {}
    names = [n for n in queries if n not in destination_names and n not in globals_]
    for query in queries.values():
        if query.name in destination_names or query.name in globals_:
            continue
        body = " ".join(e for _, e in query.steps)
        sources: list[TableRef] = list(_fabric_tables(query, globals_))
        refs = [
            other
            for other in names
            if other != query.name
            and re.search(
                r'(?<![\w."])'
                + re.escape(other)
                + r'(?![\w"])|#"'
                + re.escape(other)
                + '"',
                body,
            )
        ]
        renames: list[tuple[str, str]] = []
        selected: list[str] = []
        added: list[tuple[str, str]] = []
        unparsed = 0
        external: TableRef | None = None
        for step_name, expression in query.steps:
            if step_name in query.consts:
                continue
            fabric_sql = None
            if "Sql.Database" in expression:
                fabric_sql = fabric_sql_table(
                    f"{expression}\n{body}"
                ) or _fabric_sql_endpoint(expression, body)
            if fabric_sql is not None:
                sources.append(fabric_sql)
                continue
            if any(c in expression for c in _FABRIC_CONNECTORS):
                continue
            found, _function = _source_from(expression, body)
            if (
                found is not None
                and external is None
                and not found.connection.get("kind") == "lakehouse"
            ):
                external = found
                continue
            if _RENAME.search(expression):
                renames.extend(
                    (m.group("old"), m.group("new")) for m in _PAIR.finditer(expression)
                )
                continue
            select_match = _SELECT.search(expression)
            if select_match:
                selected.extend(_QUOTED.findall(select_match.group("cols")))
                continue
            add_match = _ADD.search(expression)
            if add_match:
                added.append((add_match.group("name"), add_match.group("expr")[:120]))
                continue
            if (
                "Table.TransformColumnTypes" in expression
                or "Table.PromoteHeaders" in expression
            ):
                continue
            if _NAVIGATION.search(expression) or re.search(r"\{\s*\[", expression):
                continue
            # A step that is only a reference to another query or step.
            if re.fullmatch(r'#?"?[\w .]+"?', expression.strip()):
                continue
            if step_name and expression:
                unparsed += 1
        if external is not None:
            sources.append(external)
        direct[query.name] = sources
        references[query.name] = refs
        column_steps[query.name] = (renames, selected, added)
        unparsed_by[query.name] = unparsed

    # Leaf sources through query references (staging queries, merges, appends).
    memo: dict[str, list[TableRef]] = {}

    def leaves(name: str, seen: frozenset) -> list[TableRef]:
        if name in memo:
            return memo[name]
        if name in seen:
            return []
        out = list(direct.get(name, []))
        for other in references.get(name, []):
            out.extend(leaves(other, seen | {name}))
        unique: dict[str, TableRef] = {}
        for ref in out:
            unique.setdefault(ref.key(), ref)
        memo[name] = list(unique.values())
        return memo[name]

    flow = _ColumnFlow(
        {n: q for n, q in queries.items() if n in direct}, direct, references
    )

    for name in direct:
        sources = leaves(name, frozenset())
        result.reads.extend(sources)
        target_entry = targets.get(name)
        if target_entry is None and (name in legacy or name.lower() in legacy):
            entry = legacy.get(name) or legacy.get(name.lower())
            target_entry = (entry["table"], entry["mode"])
        if target_entry is None:
            # "Enter data" queries count too: no source, but they can be loaded.
            for container in containers.values():
                result.dormant_writes.append(
                    Write(
                        table=TableRef(
                            workspace_id=container.workspace_id,
                            item_id=container.item_id,
                            schema=container.schema,
                            table=destination_table_name(name),
                        ),
                        mode="overwrite",
                        via_ref=f"query {name}",
                        reads=sources,
                        confidence=CONF_LOW,
                    )
                )
            if not sources and not references.get(name):
                # Neither a source nor a destination: a helper or parameter query.
                continue
            if loaded.get(name, True) and not references_to(name, references):
                result.warn(
                    f"Query {name}: no output destination found — it may be a staging query."
                )
            continue
        if unparsed_by.get(name):
            result.warn(
                f"Query {name}: {unparsed_by[name]} step(s) not understood — column "
                f"lineage may be incomplete."
            )
            result.confidence = CONF_MEDIUM

        target, raw_mode = target_entry
        mode = str(raw_mode).lower()
        mode = "overwrite" if mode in ("replace", "overwrite") else mode
        edges = flow.edges_for(name, target)
        result.writes.append(
            Write(
                table=target,
                mode=mode,
                via_ref=f"query {name}",
                reads=sources,
                column_edges=edges,
                confidence=CONF_MEDIUM,
                # Columns the chain never renames, adds or drops keep their
                # names end to end; the resolver pairs those by name.
                passthrough=True,
            )
        )
        result.column_edges.extend(edges)

    return result


_M_RENAME = re.compile(r"Table\.RenameColumns\s*\(", re.IGNORECASE)
_M_SELECT = re.compile(
    r"Table\.SelectColumns\s*\(\s*[^,]+,\s*\{(?P<cols>[^}]*)\}", re.IGNORECASE
)
_M_REMOVE = re.compile(
    r"Table\.RemoveColumns\s*\(\s*[^,]+,\s*\{(?P<cols>[^}]*)\}", re.IGNORECASE
)
_M_ADD = re.compile(
    r"Table\.AddColumn\s*\(\s*[^,]+,\s*\"(?P<name>[^\"]+)\"\s*,\s*(?P<expr>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_M_DUPLICATE = re.compile(
    r"Table\.DuplicateColumn\s*\(\s*[^,]+,\s*\"(?P<src>[^\"]+)\"\s*,\s*\"(?P<new>[^\"]+)\"",
    re.IGNORECASE,
)
_M_JOIN = re.compile(
    r"Table\.(?:Nested|Fuzzy(?:Nested)?)?Join\s*\(\s*[^,]+,\s*(?:\{[^}]*\}|\"[^\"]*\")\s*,\s*"
    r"(?P<other>#\"[^\"]+\"|[A-Za-z_][\w.]*)\s*,\s*(?:\{[^}]*\}|\"[^\"]*\")\s*,\s*\"(?P<col>[^\"]+)\"",
    re.IGNORECASE,
)
_M_EXPAND = re.compile(
    r"Table\.ExpandTableColumn\s*\(\s*[^,]+,\s*\"(?P<col>[^\"]+)\"\s*,\s*\{(?P<olds>[^}]*)\}"
    r"(?:\s*,\s*\{(?P<news>[^}]*)\})?",
    re.IGNORECASE,
)
_M_COMBINE = re.compile(r"Table\.Combine\s*\(\s*\{(?P<parts>[^}]*)\}", re.IGNORECASE)
_M_FIELD = re.compile(r"\[(?P<col>[^\[\]]+)\]")


class _ColumnFlow:
    """
    Column lineage through a dataflow's chain of queries.

    For each query, a single pass over its steps records how output columns
    relate to its input: renamed, added from other columns, duplicated,
    expanded from a joined query, dropped. `origins(query, column)` then walks
    those relations down through referenced queries (staging queries, merges,
    appends) to the column of the real source — a lakehouse table, an external
    system. Columns nothing touched keep their name and are left to the
    resolver's pass-through pairing.
    """

    def __init__(self, queries: dict, direct: dict, references: dict):
        self.queries = queries
        self.direct = direct
        self.references = references
        self._analysis: dict[str, dict] = {}

    # -- per-query analysis ----------------------------------------------
    def _inputs(self, name: str, body: str) -> list[tuple[str, object]]:
        """The query's input: its own sources, else the query its first step names."""
        if self.direct.get(name):
            return [("source", src) for src in self.direct[name]]
        query = self.queries.get(name)
        combine = _M_COMBINE.search(body)
        if combine:
            # A part may be a step of this query (`Table.Combine({Source, Other})`)
            # rather than a query; follow the step back to the query it starts from.
            steps = {_clean_name(s): e for s, e in (query.steps if query else [])}
            inputs = []
            for part in combine.group("parts").split(","):
                part = _clean_name(part)
                for _ in range(len(steps) + 1):
                    if part in self.queries or part not in steps:
                        break
                    part = _clean_name(
                        steps[part].strip().split("{")[0].split("(")[0].strip()
                    )
                if part in self.queries and part != name:
                    inputs.append(("query", part))
            return inputs
        for _step, expression in query.steps if query else []:
            candidate = _clean_name(expression.strip().split("{")[0].split("(")[0].strip())
            if candidate in self.queries and candidate != name:
                return [("query", candidate)]
            for ref in self.references.get(name, []):
                if re.search(
                    r'(?<![\w."])'
                    + re.escape(ref)
                    + r'(?![\w"])|#"'
                    + re.escape(ref)
                    + '"',
                    expression,
                ):
                    return [("query", ref)]
        return []

    def _analyse(self, name: str) -> dict:
        if name in self._analysis:
            return self._analysis[name]
        query = self.queries.get(name)
        info = {"explicit": {}, "dropped": set(), "keep": None, "inputs": []}
        self._analysis[name] = info
        if query is None:
            return info
        body = " ".join(e for _, e in query.steps)
        info["inputs"] = self._inputs(name, body)
        joins: dict[str, str] = {}

        # A reference is (kind, query, column, transform): kind "in" is the
        # query's own input (query None), "join" a column of a joined query.
        def current(column: str) -> list[tuple]:
            key = column.lower()
            if key in info["explicit"]:
                return list(info["explicit"][key][1])
            if key in info["dropped"] or (
                info["keep"] is not None and key not in info["keep"]
            ):
                return []
            return [("in", None, column, "")]

        def with_transform(refs: list[tuple], transform: str) -> list[tuple]:
            return [(kind, q, col, t or transform) for kind, q, col, t in refs]

        for _step, expression in query.steps:
            if _M_RENAME.search(expression):
                for m in _PAIR.finditer(expression):
                    old, new = m.group("old"), m.group("new")
                    refs = with_transform(current(old), "rename")
                    # Pop the old name before storing the new one: a case-only
                    # rename (Currency -> currency) is the same key.
                    info["explicit"].pop(old.lower(), None)
                    info["explicit"][new.lower()] = (new, refs)
                    if old.lower() != new.lower():
                        info["dropped"].add(old.lower())
                    info["dropped"].discard(new.lower())
                continue
            select = _M_SELECT.search(expression)
            if select:
                keep = {c.lower() for c in _QUOTED.findall(select.group("cols"))}
                info["keep"] = keep if info["keep"] is None else info["keep"] & keep
                continue
            remove = _M_REMOVE.search(expression)
            if remove:
                info["dropped"] |= {
                    c.lower() for c in _QUOTED.findall(remove.group("cols"))
                }
                continue
            duplicate = _M_DUPLICATE.search(expression)
            if duplicate:
                info["explicit"][duplicate.group("new").lower()] = (
                    duplicate.group("new"),
                    current(duplicate.group("src")),
                )
                continue
            join = _M_JOIN.search(expression)
            if join:
                joins[join.group("col").lower()] = _clean_name(join.group("other"))
            expand = _M_EXPAND.search(expression)
            if expand:
                other = joins.get(expand.group("col").lower())
                olds = _QUOTED.findall(expand.group("olds"))
                news = _QUOTED.findall(expand.group("news") or "") or olds
                for old, new in zip(olds, news, strict=False):
                    if other:
                        info["explicit"][new.lower()] = (new, [("join", other, old, "")])
                continue
            add = _M_ADD.search(expression)
            if add:
                column, text = add.group("name"), add.group("expr")[:160]
                refs = []
                for dep in dict.fromkeys(m.group("col") for m in _M_FIELD.finditer(text)):
                    refs.extend((kind, q, col, text) for kind, q, col, _t in current(dep))
                info["explicit"][column.lower()] = (column, refs)
                info["dropped"].discard(column.lower())
        return info

    # -- walking down ---------------------------------------------------------
    def origins(
        self, name: str, column: str, depth: int = 0
    ) -> list[tuple[TableRef, str, str]]:
        """(source table, source column, transform) for one output column."""
        if depth > 12:
            return []
        info = self._analyse(name)
        key = column.lower()
        if key in info["explicit"]:
            refs = info["explicit"][key][1]
        elif key in info["dropped"] or (
            info["keep"] is not None and key not in info["keep"]
        ):
            return []
        else:
            refs = [("in", None, column, "")]
        out: list[tuple[TableRef, str, str]] = []
        for ref_kind, other, col, transform in refs:
            if ref_kind == "join":
                out.extend(
                    (t, c, transform or tr)
                    for t, c, tr in self.origins(other, col, depth + 1)
                )
                continue
            for kind, value in info["inputs"]:
                if kind == "source":
                    out.append((value, col, transform))
                else:
                    out.extend(
                        (t, c, transform or tr)
                        for t, c, tr in self.origins(value, col, depth + 1)
                    )
        return out

    def named_columns(self, name: str, depth: int = 0) -> list[str]:
        """Every output column the chain names explicitly (renamed, added, kept, expanded)."""
        if depth > 12:
            return []
        info = self._analyse(name)
        names = [display for display, _refs in info["explicit"].values()]
        if info["keep"] is not None:
            names.extend(info["keep"])
        for kind, value in info["inputs"]:
            if kind == "query":
                names.extend(self.named_columns(value, depth + 1))
        seen, out = set(), []
        for n in names:
            if n.lower() in seen or n.lower() in info["dropped"]:
                continue
            if (
                info["keep"] is not None
                and n.lower() not in info["keep"]
                and n.lower() not in info["explicit"]
            ):
                continue
            seen.add(n.lower())
            out.append(n)
        return out

    def edges_for(self, name: str, target: TableRef) -> list[ColumnEdgeRef]:
        edges: list[ColumnEdgeRef] = []
        for column in self.named_columns(name):
            found = self.origins(name, column)
            confidence = (
                CONF_MEDIUM if len({(t.key(), c) for t, c, _ in found}) == 1 else CONF_LOW
            )
            for table, source_column, transform in found:
                edges.append(
                    ColumnEdgeRef(
                        source=ColumnRef(table=table, column=source_column),
                        target=ColumnRef(table=target, column=column),
                        transform=transform,
                        confidence=confidence,
                    )
                )
        unique = {}
        for e in edges:
            unique.setdefault(
                (e.source.table.key(), e.source.column.lower(), e.target.column.lower()), e
            )
        return list(unique.values())


_TABLE_NAME_UNSAFE = re.compile(r"[^\w ]")


def destination_table_name(query_name: str) -> str:
    """
    The lakehouse table a default destination creates for a query.

    Fabric keeps letters, digits, `_` and spaces and turns every other character
    into `_`: the query `orders (2)` lands as the table `orders _2_`.
    """
    return _TABLE_NAME_UNSAFE.sub("_", query_name.strip())


def references_to(name: str, references: dict[str, list[str]]) -> bool:
    """Whether another query reads this one — then it is staging, not a gap."""
    return any(name in refs for refs in references.values())


def _load_enabled(query_metadata) -> dict[str, bool]:
    if isinstance(query_metadata, str):
        try:
            query_metadata = json.loads(query_metadata)
        except json.JSONDecodeError:
            return {}
    if not isinstance(query_metadata, dict):
        return {}
    entries = query_metadata.get("queriesMetadata") or {}
    if not isinstance(entries, dict):
        return {}
    return {
        name: bool(entry.get("loadEnabled", True))
        for name, entry in entries.items()
        if isinstance(entry, dict)
    }


def _destinations(query_metadata: dict) -> dict[str, dict]:
    """
    query name -> {table, mode}, from queryMetadata.json.

    Gen2 records the output destination outside the M, under either
    `queriesMetadata` or a top-level `queries` list depending on vintage.
    """
    out: dict[str, dict] = {}
    if isinstance(query_metadata, str):
        try:
            query_metadata = json.loads(query_metadata)
        except json.JSONDecodeError:
            return out
    if not isinstance(query_metadata, dict):
        return out

    entries = query_metadata.get("queriesMetadata") or query_metadata.get("queries") or {}
    items = (
        entries.items()
        if isinstance(entries, dict)
        else [
            ((e or {}).get("queryName") or (e or {}).get("name") or "", e) for e in entries
        ]
    )

    for name, entry in items:
        if not isinstance(entry, dict):
            continue
        destination = (
            entry.get("dataDestination")
            or entry.get("destination")
            or entry.get("outputDestination")
            or {}
        )
        if not isinstance(destination, dict):
            continue
        settings = destination.get("typeProperties") or destination
        table = settings.get("tableName") or settings.get("table") or ""
        schema = settings.get("schemaName") or settings.get("schema") or ""
        lakehouse = (
            settings.get("itemName")
            or settings.get("lakehouseName")
            or settings.get("artifactName")
            or ""
        )
        if not table:
            continue
        if "." in str(table) and not schema:
            schema, _, table = str(table).partition(".")
        mode = str(
            destination.get("updateMethod") or settings.get("updateMethod") or "replace"
        ).lower()
        out[str(name)] = {
            "table": TableRef(
                lakehouse=str(lakehouse), schema=str(schema), table=str(table)
            ),
            "mode": "overwrite" if mode in ("replace", "overwrite") else mode,
        }
    return out
