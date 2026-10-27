"""
SQL parsing via sqlglot, dialect `spark`.

Used for `%%sql` cells, `spark.sql("…")` string literals, and pipeline
Script/StoredProcedure activities. sqlglot does the heavy lifting; what this
module adds is the mapping from its AST to our TableRef/ColumnEdgeRef vocabulary
and the statement shapes Fabric code actually uses:

    CREATE [OR REPLACE] TABLE x AS SELECT …
    INSERT INTO / INSERT OVERWRITE x SELECT …
    MERGE INTO x USING y ON … WHEN MATCHED …
    CREATE [OR REPLACE] VIEW

Column lineage comes from sqlglot.lineage() per output column, which resolves
through CTEs and subqueries. It needs a schema to be fully accurate; without
one it still resolves unambiguous cases, which is most real code.
"""

import logging
import re

from .base import (
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    ColumnEdgeRef,
    ColumnRef,
    ParseResult,
    TableRef,
    Write,
    parse_qualified,
)

log = logging.getLogger("lineage.parse.sql")

DIALECT = "spark"


def _table_ref(table) -> TableRef:
    """sqlglot Table -> TableRef."""
    import sqlglot.expressions as exp

    if not isinstance(table, exp.Table):
        return TableRef()
    name = table.name or ""
    schema = table.db or ""
    catalog = table.catalog or ""
    return TableRef(lakehouse=catalog, schema=schema, table=name)


def _source_tables(scope) -> list[TableRef]:
    """
    Every physical table read by an expression.

    CTE names are excluded: a CTE is not a table, and emitting one as a source
    produces a phantom node that appears in the graph and never resolves.
    """
    import sqlglot.expressions as exp

    cte_names = {
        cte.alias_or_name.lower() for cte in scope.find_all(exp.CTE) if cte.alias_or_name
    }
    out: list[TableRef] = []
    seen: set[str] = set()
    for table in scope.find_all(exp.Table):
        ref = _table_ref(table)
        if not ref.table or ref.table.lower() in cte_names:
            continue
        key = ref.key()
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


def _alias_map(statement) -> dict[str, TableRef]:
    """
    Alias -> real table, for every table in the statement.

    Without this, `FROM lh_silver.sales.sales_lines l` makes sqlglot report a
    column's source as `l.net_amount`, and the edge points at a table called
    "l" that does not exist — the lineage looks complete while every source
    node is a phantom.
    """
    import sqlglot.expressions as exp

    out: dict[str, TableRef] = {}
    for table in statement.find_all(exp.Table):
        ref = _table_ref(table)
        if not ref.table:
            continue
        alias = table.alias_or_name
        if alias:
            out[alias.lower()] = ref
        # Also key on the bare table name: an unaliased `FROM fx_rates` is
        # referenced as `fx_rates.currency`.
        out.setdefault(ref.table.lower(), ref)
    return out


def _is_star(projection) -> bool:
    """`*` and `alias.*` alike — sqlglot wraps the qualified form in a Column."""
    import sqlglot.expressions as exp

    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
    )


def _column_lineage(
    statement,
    target: TableRef,
    sources: list[TableRef],
    result: ParseResult,
    dialect: str = DIALECT,
) -> tuple[list[ColumnEdgeRef], bool]:
    """
    Per-output-column lineage via sqlglot.lineage(). Returns (edges, has_star).

    Runs one call per output column. It can raise for all sorts of legitimate
    SQL (an unresolvable star, a UDF, a dialect edge case), so each column is
    guarded independently — losing one column's lineage must not cost the others
    or the table-level edge. A star is not traced here: it is reported so the
    write is marked pass-through, and the resolver pairs the star's columns by
    name against the real schema.
    """
    import sqlglot.expressions as exp
    from sqlglot.lineage import lineage as sqlglot_lineage

    if isinstance(statement, exp.Merge):
        return _merge_columns(statement, target, sources), False

    select = statement.find(exp.Select)
    if select is None:
        return [], False

    has_star = any(_is_star(e) for e in select.expressions)
    # INSERT INTO t (a, b) SELECT x, y — the target columns are a and b, by position.
    insert_columns: list[str] = []
    if isinstance(statement, exp.Insert) and isinstance(statement.this, exp.Schema):
        insert_columns = [c.name for c in statement.this.expressions if c.name]

    edges: list[ColumnEdgeRef] = []
    single_source = sources[0] if len(sources) == 1 else None
    aliases = _alias_map(statement)

    for position, projection in enumerate(select.expressions):
        if _is_star(projection):
            continue
        name = projection.alias_or_name
        positional = (
            insert_columns[position]
            if insert_columns and position < len(insert_columns) and not has_star
            else ""
        )
        if not name and not positional:
            continue
        # With an INSERT column list the position names the target, even for
        # an unaliased expression like `y + 1`.
        target_name = positional or name
        root = None
        if name:
            try:
                root = sqlglot_lineage(
                    name, select if insert_columns else statement, dialect=dialect
                )
            except Exception as e:  # noqa: BLE001 — sqlglot raises broadly here
                result.warn(f"Could not trace column {name}: {type(e).__name__}")

        transform = _transform_text(projection)
        found = False
        for leaf in _leaves(root) if root is not None else []:
            source_table, source_column = _split_leaf(leaf, single_source, aliases)
            if source_table is None or not source_column or source_column == "*":
                continue
            found = True
            edges.append(
                ColumnEdgeRef(
                    source=ColumnRef(table=source_table, column=source_column),
                    target=ColumnRef(table=target, column=target_name),
                    transform=transform,
                    confidence=CONF_HIGH,
                )
            )
        if not found:
            # Unresolved: attribute the referenced columns through their table
            # alias when there is one, else to the single source — flagged low.
            for column in projection.find_all(exp.Column):
                if _is_star(column):
                    continue
                owner = aliases.get((column.table or "").lower()) if column.table else None
                owner = owner or single_source
                if owner is None:
                    continue
                edges.append(
                    ColumnEdgeRef(
                        source=ColumnRef(table=owner, column=column.name),
                        target=ColumnRef(table=target, column=target_name),
                        transform=transform,
                        confidence=CONF_LOW,
                    )
                )
    return edges, has_star


def _merge_columns(
    statement, target: TableRef, sources: list[TableRef]
) -> list[ColumnEdgeRef]:
    """
    MERGE: `WHEN MATCHED THEN UPDATE SET t.a = s.x` and
    `WHEN NOT MATCHED THEN INSERT (a, b) VALUES (s.x, s.y)` — each assignment is
    a column edge from the source side to the target column.
    """
    import sqlglot.expressions as exp

    aliases = _alias_map(statement)
    target_aliases = {
        (t.alias or t.name or "").lower()
        for t in statement.find_all(exp.Table)
        if _table_ref(t).key() == target.key()
    }
    single_source = sources[0] if len(sources) == 1 else None
    edges: list[ColumnEdgeRef] = []

    def origins(expression) -> list[tuple[TableRef, str]]:
        out = []
        for column in expression.find_all(exp.Column):
            table_alias = (column.table or "").lower()
            if table_alias in target_aliases:
                continue  # the target's own column on the right-hand side
            owner = aliases.get(table_alias) if table_alias else single_source
            if owner is not None and column.name:
                out.append((owner, column.name))
        return out

    def add(target_column: str, expression) -> None:
        text = expression.sql()
        for owner, column in origins(expression):
            edges.append(
                ColumnEdgeRef(
                    source=ColumnRef(table=owner, column=column),
                    target=ColumnRef(table=target, column=target_column),
                    transform="" if text.split(".")[-1] == column else text,
                    confidence=CONF_HIGH,
                )
            )

    for when in statement.find_all(exp.When):
        action = when.args.get("then")
        if isinstance(action, exp.Update):
            for assignment in action.expressions:
                if isinstance(assignment, exp.EQ) and isinstance(
                    assignment.this, exp.Column
                ):
                    add(assignment.this.name, assignment.expression)
        elif isinstance(action, exp.Insert):
            columns = action.this.expressions if isinstance(action.this, exp.Tuple) else []
            values = (
                action.expression.expressions
                if isinstance(action.expression, exp.Tuple)
                else []
            )
            for column, value in zip(columns, values, strict=False):
                if isinstance(column, exp.Column | exp.Identifier):
                    add(column.name if hasattr(column, "name") else str(column), value)
    return edges


def _leaves(root):
    """Leaf nodes of a sqlglot lineage tree — the actual source columns."""
    stack = [root]
    seen = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        downstream = getattr(current, "downstream", None) or []
        if not downstream:
            yield current
            continue
        stack.extend(downstream)


def _split_leaf(
    leaf, fallback: TableRef | None, aliases: dict[str, TableRef]
) -> tuple[TableRef | None, str]:
    """
    A lineage leaf's name is `table.column` or just `column`.

    The table part is usually an ALIAS, so it is resolved through the alias map
    before falling back to treating it as a real name.
    """
    name = getattr(leaf, "name", "") or ""
    if "." in name:
        table_part, _, column = name.rpartition(".")
        resolved = aliases.get(table_part.lower())
        if resolved is not None:
            return resolved, column
        return parse_qualified(table_part), column
    source = getattr(leaf, "source", None)
    if source is not None:
        ref = _table_ref(source)
        if ref.table:
            return aliases.get(ref.table.lower(), ref), name
    return fallback, name


def _transform_text(projection) -> str:
    """
    The expression behind an output column, for the trace card.

    A plain passthrough or rename gets a word rather than its own SQL echoed
    back: `customer_id` as the "transformation" for `customer_id` is noise.
    """
    import sqlglot.expressions as exp

    inner = projection.this if isinstance(projection, exp.Alias) else projection
    if isinstance(inner, exp.Column):
        return "rename" if isinstance(projection, exp.Alias) else "passthrough"
    try:
        return inner.sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001
        return ""


def _write_mode(statement) -> str:
    import sqlglot.expressions as exp

    if isinstance(statement, exp.Create):
        kind = (statement.args.get("kind") or "").upper()
        if kind == "VIEW":
            return "create_view"
        return "replace" if statement.args.get("replace") else "create"
    if isinstance(statement, exp.Insert):
        return "overwrite" if statement.args.get("overwrite") else "append"
    if isinstance(statement, exp.Merge):
        return "merge"
    if isinstance(statement, exp.Update):
        return "update"
    if isinstance(statement, exp.Delete):
        return "delete"
    if isinstance(statement, exp.Select) and statement.args.get("into"):
        return "create"
    return ""


_MODULE_HEAD = re.compile(
    r"^\s*(?:CREATE|ALTER|CREATE\s+OR\s+ALTER)\s+(?:PROC|PROCEDURE|FUNCTION|TRIGGER)\b.*?\bAS\b",
    re.IGNORECASE | re.DOTALL,
)


def tsql_module_body(text: str) -> str:
    """
    The statements inside a T-SQL procedure / function, without its header.

    `CREATE PROCEDURE dbo.load AS BEGIN INSERT …; UPDATE …; END` — the
    procedure itself is not a table; the INSERT and UPDATE targets are.
    """
    match = _MODULE_HEAD.match(text or "")
    if not match:
        return text
    body = text[match.end() :].strip()
    if body.upper().startswith("BEGIN"):
        body = body[5:]
        end = body.upper().rfind("END")
        if end >= 0:
            body = body[:end]
    return body


# Fabric's materialized lake views (Spark SQL), which sqlglot does not know:
#   CREATE [OR REPLACE] MATERIALIZED LAKE VIEW [IF NOT EXISTS] silver.x
#     [(CONSTRAINT c CHECK (…) ON MISMATCH DROP)] [PARTITIONED BY (…)]
#     [COMMENT "…"] [TBLPROPERTIES (…)] AS SELECT …
# The view is a table its SELECT keeps refreshed, so for lineage it is a CTAS.
_MLV = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?MATERIALIZED\s+LAKE\s+VIEW\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[\w.`\[\]\"]+)"
    r"(?P<options>.*?)\bAS\s+(?=SELECT\b|WITH\b|\()",
    re.IGNORECASE | re.DOTALL,
)
# Statements about a materialized lake view that move no data between tables.
_MLV_HOUSEKEEPING = re.compile(
    r"^\s*(?:REFRESH|DROP|ALTER)\s+MATERIALIZED\s+LAKE\s+VIEW\b", re.IGNORECASE
)
# A statement sqlglot could only keep as raw text ("Command"). Those starting
# with one of these may write a table, so they are named, never dropped quietly.
_WRITING_COMMANDS = {"CREATE", "INSERT", "MERGE", "UPDATE", "DELETE", "REPLACE", "COPY"}


def _rewrite_mlv(text: str) -> tuple[str, set[str]]:
    """Materialized lake views as CTAS, and the names of the views rewritten."""
    names: set[str] = set()

    def as_ctas(match: re.Match) -> str:
        name = match.group("name")
        names.add(
            name.replace("`", "").replace("[", "").replace("]", "").replace('"', "").lower()
        )
        return f"CREATE OR REPLACE TABLE {name} AS "

    return _MLV.sub(as_ctas, text), names


def parse_sql(text: str, *, via_ref: str = "", dialect: str = DIALECT) -> ParseResult:
    """
    Parse one or more SQL statements into reads, writes and column edges.

    Statement-level failures are isolated: a notebook with one unparseable cell
    should still yield lineage for the other nine. `dialect="tsql"` for
    warehouse code, where a procedure body is unwrapped first.
    """
    result = ParseResult()
    if not text or not text.strip():
        return result

    import sqlglot
    import sqlglot.expressions as exp

    if dialect == "tsql":
        text = tsql_module_body(text)
    text, lake_views = _rewrite_mlv(text)
    try:
        statements = sqlglot.parse(text, dialect=dialect)
    except Exception as e:  # noqa: BLE001 — sqlglot raises many types
        result.warn(f"SQL did not parse: {type(e).__name__}: {str(e)[:160]}")
        result.confidence = CONF_LOW
        return result

    for statement in statements:
        if statement is None:
            continue
        if isinstance(statement, exp.Command):
            _warn_command(statement, result)
            continue
        try:
            _parse_statement(statement, result, via_ref, exp, dialect)
        except Exception as e:  # noqa: BLE001 — never lose the whole artifact
            log.debug("statement parse failed", exc_info=True)
            result.warn(f"Statement skipped: {type(e).__name__}")
            result.confidence = CONF_MEDIUM

    for write in result.writes:
        key = write.table.key()
        if any(
            key == v or key.endswith(f".{v}") or v.endswith(f".{key}") for v in lake_views
        ):
            write.mode = "materialized_view"
    return result


def _warn_command(statement, result: ParseResult) -> None:
    """A statement sqlglot kept only as text: say so if it may write a table."""
    try:
        text = statement.sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001
        text = str(statement.this or "")
    if _MLV_HOUSEKEEPING.match(text):
        return
    keyword = str(statement.this or "").split()[0].upper() if statement.this else ""
    if keyword in _WRITING_COMMANDS:
        snippet = " ".join(text.split())[:100]
        result.warn(f"Statement not understood, its lineage is missing: {snippet}")
        result.confidence = CONF_MEDIUM


def _parse_statement(
    statement, result: ParseResult, via_ref: str, exp, dialect: str = DIALECT
) -> None:
    target: TableRef | None = None

    if isinstance(statement, exp.Create):
        table = statement.this
        if isinstance(table, exp.Schema):
            table = table.this
        target = _table_ref(table)
    elif isinstance(statement, exp.Insert):
        table = statement.this
        if isinstance(table, exp.Schema):
            table = table.this
        target = _table_ref(table)
    elif isinstance(statement, exp.Merge):
        target = _table_ref(statement.this)
    elif isinstance(statement, exp.Update | exp.Delete):
        target = _table_ref(statement.this)
        # `UPDATE d SET … FROM dbo.dim d` names the alias; the table is dbo.dim.
        if target.table and not target.schema:
            aliased = {
                (t.alias or "").lower(): _table_ref(t)
                for t in statement.find_all(exp.Table)
                if t.alias
            }
            target = aliased.get(target.table.lower(), target)
    elif isinstance(statement, exp.Select) and statement.args.get("into"):
        # T-SQL `SELECT … INTO dbo.snapshot FROM …` creates the table.
        into = statement.args["into"]
        target = _table_ref(into.this if hasattr(into, "this") else into)

    sources = _source_tables(statement)
    if isinstance(statement, exp.Update | exp.Delete):
        # The UPDATE target also appears (by alias) among its own sources.
        aliases = {
            (t.alias or t.name or "").lower()
            for t in statement.find_all(exp.Table)
            if _table_ref(t).key() == (target.key() if target else "")
        }
        sources = [s for s in sources if s.table.lower() not in aliases or s.schema]
    if target is not None and target.table:
        # The target of a MERGE also appears in its own FROM; it is the
        # destination, not an input.
        sources = [s for s in sources if s.key() != target.key()]

    if target is None or not target.table:
        # A plain SELECT: reads only. Worth recording — it is what makes a table
        # "used by" something even when nothing is written.
        result.reads.extend(sources)
        return

    column_edges, has_star = _column_lineage(statement, target, sources, result, dialect)
    result.writes.append(
        Write(
            table=target,
            mode=_write_mode(statement),
            via_ref=via_ref,
            reads=sources,
            column_edges=column_edges,
            confidence=CONF_HIGH if column_edges or not sources else CONF_MEDIUM,
            # A star, or a CTAS / INSERT with no traceable projection, copies
            # columns by name — the resolver pairs them against the schema.
            passthrough=has_star or not column_edges,
        )
    )
    result.column_edges.extend(column_edges)
