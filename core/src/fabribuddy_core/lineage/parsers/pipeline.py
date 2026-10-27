"""
Data pipeline parser (`pipeline-content.json`).

Walks activities recursively — ForEach, IfCondition, Until, Switch and
ExecutePipeline all nest their children in different places, and an ingest
pipeline almost always has its Copy activities inside a ForEach. Missing the
recursion means missing the entire bronze layer.

Handled activity types:
  Copy          -> external source + lakehouse sink, plus translator mappings
  TridentNotebook / SynapseNotebook -> a notebook invocation with its parameters
  RefreshDataflow            -> a dataflow invocation
  Script / SqlServerStoredProcedure -> SQL, via sqlglot
  ExecutePipeline            -> another pipeline
"""

import logging
import re
from contextvars import ContextVar

from .base import (
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    ColumnEdgeRef,
    ColumnRef,
    ParseResult,
    TableRef,
    Write,
)
from .sql import parse_sql

log = logging.getLogger("lineage.parse.pipeline")

# Where each container type keeps its children.
_CHILD_PATHS = (
    ("activities",),
    ("ifTrueActivities",),
    ("ifFalseActivities",),
    ("defaultActivities",),
    ("cases",),
)

# Data Factory expressions embedded in a SQL string.
_EXPRESSION = re.compile(r"@\{[^}]*\}")
_FROM = re.compile(r"\bFROM\s+(?P<table>[\w\[\]\"`.]+)", re.IGNORECASE)

# `@pipeline().libraryVariables.<name>` -> its Variable Library value, for the
# pipeline being parsed (set by parse_pipeline, read by _unwrap).
_LIBRARY: ContextVar[dict[str, str] | None] = ContextVar("pipeline_library", default=None)
_LIBRARY_REF = re.compile(r"pipeline\(\)\.libraryVariables\.(?P<name>[A-Za-z_][\w]*)")

_COPY_TYPES = {"copy"}
_NOTEBOOK_TYPES = {"tridentnotebook", "synapsenotebook", "notebook"}
_SCRIPT_TYPES = {"script", "sqlserverstoredprocedure", "lookup"}


def _activities(node) -> list[dict]:
    """Every activity in the tree, flattened, containers included."""
    out: list[dict] = []

    def walk(item):
        if isinstance(item, list):
            for entry in item:
                walk(entry)
            return
        if not isinstance(item, dict):
            return
        if item.get("type") and item.get("name"):
            out.append(item)
        properties = item.get("typeProperties") or {}
        for container in (item, properties):
            for path in _CHILD_PATHS:
                value = container.get(path[0])
                if value:
                    walk(value)
        # Switch cases carry their own activity lists.
        for case in properties.get("cases") or []:
            walk((case or {}).get("activities") or [])

    walk(node)
    return out


def _connection_detail(source: dict, dataset: dict | None) -> dict:
    """
    Connection fingerprint for an external source.

    Only genuinely identifying fields go in: the same server reached with two
    different timeouts is one system, and including incidental properties would
    split it into two nodes.
    """
    properties = (dataset or {}).get("typeProperties") or {}
    linked = (dataset or {}).get("linkedService") or {}
    detail = {
        "kind": source.get("type") or "",
        "server": source.get("server")
        or properties.get("server")
        or linked.get("server")
        or "",
        "database": source.get("database")
        or properties.get("database")
        or linked.get("database")
        or "",
        "url": source.get("url") or properties.get("url") or linked.get("url") or "",
        "connection": (source.get("datasetSettings") or {})
        .get("externalReferences", {})
        .get("connection", ""),
    }
    return {k: v for k, v in detail.items() if v}


def _source_object(source: dict, dataset: dict | None) -> tuple[str, str]:
    """
    (object name, the query it came from) for a Copy source.

    A `sqlReaderQuery` is parsed for its table rather than used as the object
    name — `SELECT * FROM dbo.CUSTTABLE WHERE …` names CUSTTABLE, and storing
    the query as the node name would make a different node every time the
    watermark changed.
    """
    properties = (dataset or {}).get("typeProperties") or {}
    query = (
        source.get("sqlReaderQuery")
        or source.get("query")
        or source.get("sqlReaderStoredProcedureName")
        or ""
    )

    explicit = (
        source.get("table")
        or properties.get("table")
        or properties.get("tableName")
        or properties.get("fileName")
        # OData / REST name the entity by path (`BillingBudgets`).
        or properties.get("path")
        or properties.get("relativeUrl")
        or ""
    )
    if isinstance(explicit, dict) and "value" in explicit:
        explicit = _unwrap(explicit)[0]
    if isinstance(explicit, dict):
        schema = explicit.get("schema") or ""
        name = explicit.get("table") or explicit.get("name") or ""
        explicit = f"{schema}.{name}".strip(".")
    if explicit:
        return str(explicit), query

    if query:
        # `@{pipeline().parameters.watermark}` is a Data Factory expression, not
        # SQL, and it makes sqlglot fail on the whole statement. Substituting a
        # placeholder keeps the FROM clause — which is the part naming the
        # source table — readable.
        sanitised = _EXPRESSION.sub("'?'", query)
        parsed = parse_sql(sanitised)
        tables = [t for t in parsed.reads if t.table]
        if tables:
            first = tables[0]
            return (
                ".".join(p for p in (first.schema, first.table) if p),
                query,
            )
        # sqlglot could not parse it even sanitised; a FROM clause is still
        # worth reading directly rather than giving up on the source's identity.
        fallback = _FROM.search(sanitised)
        if fallback:
            return fallback.group("table").strip('[]"`'), query
    return "", query


# Linked-service types that are Fabric items, addressed by workspace + artifact id.
_FABRIC_ITEM_TYPES = {"lakehouse", "datawarehouse", "warehouse", "sqldatabase"}


def _unwrap(value) -> tuple[str, str]:
    """
    (literal, expression) from a pipeline property.

    Fabric wraps many properties as `{"value": …, "type": "Expression"}`. A
    value starting with `@` is a Data Factory expression evaluated at run time
    (`@item().table`), which is dynamic; anything else is a plain literal that
    merely happens to be wrapped.
    """
    if isinstance(value, dict) and "value" in value:
        value = value.get("value")
    if value is None or isinstance(value, dict | list):
        return "", ""
    text = _resolve_library(str(value))
    if text.startswith("@"):
        return "", text
    return text, ""


def _resolve_library(text: str) -> str:
    """
    Substitute Variable Library references with their values in effect.

    `@pipeline().libraryVariables.lh` as the whole value, or `@{…}` inside a
    string. A reference with no known value is left as it was (dynamic).
    """
    values = _LIBRARY.get() or {}
    if not values or "libraryVariables" not in text:
        return text
    whole = re.fullmatch(r"@" + _LIBRARY_REF.pattern, text.strip())
    if whole:
        return values.get(whole.group("name").lower(), text)

    def interpolate(match: re.Match) -> str:
        name = _LIBRARY_REF.fullmatch(match.group(0)[2:-1].strip())
        if name and name.group("name").lower() in values:
            return values[name.group("name").lower()]
        return match.group(0)

    return _EXPRESSION.sub(interpolate, text)


def _fabric_item(dataset: dict | None) -> tuple[str, str]:
    """(workspace id, item id) when the dataset's linked service is a Fabric item."""
    linked = (dataset or {}).get("linkedService") or {}
    properties = linked.get("properties") or {}
    if str(properties.get("type") or "").lower() not in _FABRIC_ITEM_TYPES:
        return "", ""
    type_properties = properties.get("typeProperties") or {}
    # Either may be a Variable Library reference (an ItemReference variable).
    return (
        _unwrap(type_properties.get("workspaceId"))[0],
        _unwrap(type_properties.get("artifactId"))[0],
    )


def _fabric_table(settings: dict, dataset: dict | None) -> TableRef:
    """A lakehouse/warehouse table on either side of a Copy."""
    properties = (dataset or {}).get("typeProperties") or {}
    lakehouse = (
        (dataset or {}).get("linkedService", {}).get("name")
        or settings.get("lakehouse")
        or ""
    )
    workspace_id, item_id = _fabric_item(dataset)
    raw_table = (
        settings.get("tableName")
        or settings.get("table")
        or properties.get("table")
        or properties.get("tableName")
        or ""
    )
    raw_schema = (
        settings.get("schema")
        or properties.get("schema")
        or properties.get("schemaName")
        or ""
    )
    if isinstance(raw_table, dict) and "value" not in raw_table:
        raw_schema = raw_table.get("schema") or raw_schema
        raw_table = raw_table.get("table") or raw_table.get("name") or ""
    table, table_expr = _unwrap(raw_table)
    schema, schema_expr = (
        _unwrap(raw_schema) if not isinstance(raw_schema, list) else ("", "")
    )
    if table_expr or schema_expr:
        return TableRef(
            dynamic_expr=".".join(
                x for x in (schema_expr or schema, table_expr or table) if x
            ),
            workspace_id=workspace_id,
            item_id=item_id,
        )
    # `d365.custtable` in one field rather than two.
    if table and "." in table and not schema:
        schema, _, table = table.partition(".")
    return TableRef(
        lakehouse=lakehouse,
        schema=schema,
        table=table,
        workspace_id=workspace_id,
        item_id=item_id,
    )


def _sink_table(sink: dict, dataset: dict | None) -> TableRef:
    return _fabric_table(sink, dataset)


def _write_mode(sink: dict) -> str:
    action = (sink.get("tableActionOption") or sink.get("writeBehavior") or "").lower()
    if action in ("append", "upsert", "overwrite"):
        return action
    if action in ("overwriteschema", "replace"):
        return "overwrite"
    return "append"


def _mappings(
    translator: dict, source_table: TableRef, target: TableRef
) -> tuple[list[ColumnEdgeRef], bool]:
    """
    Column edges from `translator.mappings`.

    Returns (edges, explicit). `explicit=False` means the pipeline is
    auto-mapping, so the caller pairs columns 1:1 by name once it knows the real
    schema — inventing names here would be a guess.
    """
    mappings = translator.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        return [], False

    edges: list[ColumnEdgeRef] = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        source_name = ((mapping.get("source") or {}).get("name")) or ""
        target_name = ((mapping.get("sink") or {}).get("name")) or ""
        if not source_name or not target_name:
            continue
        edges.append(
            ColumnEdgeRef(
                source=ColumnRef(table=source_table, column=source_name),
                target=ColumnRef(table=target, column=target_name),
                transform="Copy mapping"
                if source_name != target_name
                else "Copy mapping (1:1)",
                confidence=CONF_HIGH,
            )
        )
    return edges, True


def parse_pipeline(content: dict, variables: dict[str, str] | None = None) -> ParseResult:
    """
    Parse a pipeline definition into reads, writes, column edges and invocations.

    `variables` are the workspace's Variable Library values (`library/variable`,
    lowercase); the pipeline's `libraryVariables` map its own names onto them.
    """
    if not isinstance(content, dict):
        result = ParseResult()
        result.warn("Pipeline definition was not an object.")
        result.confidence = CONF_LOW
        return result
    root = content.get("properties") or content
    token = _LIBRARY.set(_library_values(root, variables or {}))
    defaults = {
        str(name).lower(): str(spec.get("defaultValue"))
        for name, spec in (root.get("parameters") or {}).items()
        if isinstance(spec, dict)
        and spec.get("defaultValue") not in (None, "")
        and not isinstance(spec.get("defaultValue"), dict | list)
    }
    parameters_token = _PARAMETERS.set(defaults)
    try:
        return _parse(root)
    finally:
        _PARAMETERS.reset(parameters_token)
        _LIBRARY.reset(token)


def _notebook_parameters(raw) -> dict[str, str]:
    """
    {name: value} a Notebook activity passes, where the value is known: a
    literal, a library variable, or a pipeline parameter's default. A value
    only known at run time (`@item().table`) is left out — the notebook's own
    default then stands, which is what the parse can honestly say.
    """
    out: dict[str, str] = {}
    for name, spec in (raw or {}).items():
        value = spec.get("value") if isinstance(spec, dict) else spec
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        if isinstance(value, bool):
            value = "true" if value else "false"
        if value is None or isinstance(value, dict | list):
            continue
        text = _resolve_pipeline_parameter(_resolve_library(str(value)))
        if not text.startswith("@"):
            out[str(name)] = text
    return out


# This pipeline's own parameters and their defaults (set by parse_pipeline).
_PARAMETERS: ContextVar[dict[str, str] | None] = ContextVar(
    "pipeline_parameters", default=None
)
_PARAMETER_REF = re.compile(r"@pipeline\(\)\.parameters\.(?P<name>[A-Za-z_]\w*)")


def _resolve_pipeline_parameter(text: str) -> str:
    """`@pipeline().parameters.x` as the whole value -> its default, when it has one."""
    match = _PARAMETER_REF.fullmatch(text.strip())
    if not match:
        return text
    return (_PARAMETERS.get() or {}).get(match.group("name").lower(), text)


def _library_values(root: dict, variables: dict[str, str]) -> dict[str, str]:
    """The pipeline's library variable names -> their values in effect."""
    out: dict[str, str] = {}
    for name, spec in (root.get("libraryVariables") or {}).items():
        if not isinstance(spec, dict):
            continue
        key = f"{spec.get('libraryName', '')}/{spec.get('variableName', '')}".lower()
        if key in variables:
            out[str(name).lower()] = variables[key]
    return out


def _parse(root: dict) -> ParseResult:
    result = ParseResult()
    activities = _activities(root)
    if not activities:
        result.warn("No activities found in the pipeline definition.")

    for activity in activities:
        kind = (activity.get("type") or "").lower()
        name = activity.get("name") or kind
        properties = activity.get("typeProperties") or {}

        if kind in _COPY_TYPES:
            _copy_activity(activity, name, properties, result)
        elif kind in _NOTEBOOK_TYPES:
            reference = (
                properties.get("notebook")
                or (properties.get("notebookId") or "")
                or ((activity.get("reference") or {}).get("referenceName") or "")
            )
            if isinstance(reference, dict):
                reference = reference.get("referenceName") or reference.get("id") or ""
            if reference:
                result.invokes.append(str(reference))
                passed = _notebook_parameters(properties.get("parameters"))
                if passed:
                    result.invoke_parameters.append((str(reference), passed))
        elif kind == "refreshdataflow":
            reference = properties.get("dataflow") or properties.get("dataflowId") or ""
            if isinstance(reference, dict):
                reference = reference.get("referenceName") or reference.get("id") or ""
            if reference:
                result.invokes.append(str(reference))
        elif kind in ("executepipeline", "invokepipeline"):
            # ADF-style ExecutePipeline names the child; Fabric's InvokePipeline
            # carries its item id (pipelineId) and workspace.
            reference = properties.get("pipelineId") or properties.get("pipeline") or {}
            if isinstance(reference, dict):
                reference = reference.get("referenceName") or reference.get("id") or ""
            if reference:
                result.invokes.append(str(reference))
        elif kind in _SCRIPT_TYPES:
            _script_activity(properties, name, result)

    return result


def _copy_activity(
    activity: dict, name: str, properties: dict, result: ParseResult
) -> None:
    source = properties.get("source") or {}
    sink = properties.get("sink") or {}

    # Datasets are inlined in Fabric pipelines; Synapse-era ones use inputs/outputs.
    source_dataset = source.get("datasetSettings") or _first_reference(
        activity.get("inputs")
    )
    sink_dataset = sink.get("datasetSettings") or _first_reference(activity.get("outputs"))

    target = _sink_table(sink, sink_dataset)
    if target.dynamic_expr:
        # `@item().table` inside a ForEach: the destination is only known at
        # run time. Recorded as dynamic so the gap is visible, not silent.
        result.warn(
            f"{name}: Copy sink table is a run-time expression ({target.dynamic_expr})."
        )
        result.writes.append(Write(table=target, mode=_write_mode(sink), via_ref=name))
        return
    if not target.table:
        result.warn(f"{name}: could not determine the Copy sink table.")
        return

    query = ""
    if any(_fabric_item(source_dataset)):
        # Lakehouse -> lakehouse (or warehouse) Copy: the source is a table we
        # hold, not an external system.
        source_ref = _fabric_table(source, source_dataset)
    else:
        object_name, query = _source_object(source, source_dataset)
        detail = _connection_detail(source, source_dataset)
        source_ref = TableRef(
            table=object_name or "(unknown)",
            is_external=True,
            connection=detail,
        )

    # Without explicit mappings the Copy auto-maps by name: marked pass-through,
    # so the resolver pairs columns against the schema it knows rather than the
    # parser inventing column names.
    edges, explicit = _mappings(properties.get("translator") or {}, source_ref, target)

    result.writes.append(
        Write(
            table=target,
            mode=_write_mode(sink),
            via_ref=name,
            reads=[source_ref],
            column_edges=edges,
            confidence=CONF_HIGH if explicit else CONF_MEDIUM,
            # Automatic mapping copies columns by name; the resolver pairs them.
            passthrough=not explicit,
        )
    )
    result.column_edges.extend(edges)
    if query:
        # Kept so the Code tab can show the query that defined the source.
        result.warn(f"{name}: source query captured")


def _first_reference(entries) -> dict | None:
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0]
    return None


def _script_activity(properties: dict, name: str, result: ParseResult) -> None:
    scripts = properties.get("scripts") or []
    texts = [s.get("text") for s in scripts if isinstance(s, dict) and s.get("text")]
    single = properties.get("text") or properties.get("storedProcedureName")
    if single:
        texts.append(str(single))
    for text in texts:
        nested = parse_sql(str(text), via_ref=name)
        result.merge(nested)
