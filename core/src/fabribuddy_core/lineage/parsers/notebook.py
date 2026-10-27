"""
Fabric notebook parser (`notebook-content.py`).

Three jobs:

1. **Split into cells.** Fabric serialises a notebook as a Python file with
   `# MARKDOWN ********************` / `# CELL ********************` markers.
   The cell index is what the Code tab highlights, so it has to be right.
2. **Read the META block** to find the default lakehouse. Without it,
   `spark.read.table("customers")` cannot be resolved — and unqualified names
   are the norm in real notebooks, not the exception.
3. **Parse each cell**: `%%sql` and `spark.sql(...)` go to the SQL parser,
   PySpark goes through Python's `ast`.

`from pyspark.sql import functions as F` is not used anywhere in this codebase
or its fixtures — functions are imported directly.
"""

import ast
import json
import logging
import re
from urllib.parse import urlparse

from .base import (
    CONF_LOW,
    CONF_MEDIUM,
    ColumnEdgeRef,
    ColumnRef,
    ParseResult,
    TableRef,
    Write,
    parse_qualified,
)
from .sql import parse_sql

log = logging.getLogger("lineage.parse.notebook")

# Fabric's cell separator. The row of asterisks is padding and varies in length.
# Fabric writes `# METADATA ****`; older exports and our fixtures `# META ****`.
# A parameter cell is `# PARAMETERS CELL ****`: code, whose assignments are the
# defaults a pipeline's Notebook activity may override.
_CELL_MARKER = re.compile(
    r"^#\s*(PARAMETERS CELL|CELL|MARKDOWN|METADATA|META)\s*\*+\s*$", re.MULTILINE
)

_MAGIC_SQL = re.compile(r"^\s*%%sql\b", re.IGNORECASE)
_MAGIC_RUN = re.compile(r"^\s*%run\s+(?P<target>\S+)", re.MULTILINE)


class Cell:
    __slots__ = ("kind", "index", "text")

    def __init__(self, kind: str, index: int, text: str):
        self.kind = kind
        self.index = index
        self.text = text

    @property
    def label(self) -> str:
        return f"cell {self.index}"


def split_cells(source: str) -> tuple[list[Cell], dict]:
    """
    Split a notebook into cells and return (code cells, metadata).

    Markdown cells are dropped — they hold documentation, not lineage — but they
    still consume a cell index, because "cell 4" has to mean what the customer
    sees in the Fabric UI.
    """
    if not source:
        return [], {}

    parts: list[tuple[str, str]] = []
    matches = list(_CELL_MARKER.finditer(source))
    if not matches:
        # No markers: a plain .py file, or a notebook exported another way.
        return [Cell("CELL", 1, source)], {}

    preamble = source[: matches[0].start()].strip()
    # Fabric writes a `# Fabric notebook source` header before the first marker.
    # It is a file header, not a cell — counting it shifts every cell index by
    # one, so the Code tab would highlight the wrong cell. Only a preamble with
    # real code counts.
    if preamble and any(
        line.strip() and not line.lstrip().startswith("#") for line in preamble.splitlines()
    ):
        parts.append(("CELL", preamble))

    for n, match in enumerate(matches):
        kind = {"METADATA": "META", "PARAMETERS CELL": "PARAMETERS"}.get(
            match.group(1), match.group(1)
        )
        start = match.end()
        end = matches[n + 1].start() if n + 1 < len(matches) else len(source)
        parts.append((kind, source[start:end]))

    metadata: dict = {}
    cells: list[Cell] = []
    index = 0
    for kind, text in parts:
        if kind == "META":
            # The notebook-level block (kernel, attached lakehouse) comes first;
            # every cell then carries its own `{"language": …}` block, which
            # must not overwrite it — or the default lakehouse is lost.
            parsed = _parse_meta(text)
            if not metadata or "dependencies" in parsed:
                metadata = parsed if "dependencies" in parsed or not metadata else metadata
            continue
        index += 1
        if kind == "MARKDOWN":
            continue
        if text.strip():
            cells.append(Cell(kind, index, text))
    return cells, metadata


def _parse_meta(text: str) -> dict:
    """
    The META cell is JSON in a comment block. Each line is `# ...`.

    Returns {} rather than raising for anything unexpected: a notebook with a
    malformed META block still has parseable code, and unqualified names simply
    stay unresolved (and are reported as such).
    """
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("#"):
            continue
        body = stripped.lstrip("#").strip()
        # Fabric writes each metadata line as "# META <json fragment>"; the
        # keyword has to come off too or the JSON never parses and every
        # unqualified table name in the notebook silently fails to resolve.
        if body.upper().startswith("META"):
            body = body[4:].strip()
        lines.append(body)
    blob = "\n".join(lines).strip()
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        start, end = blob.find("{"), blob.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(blob[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def default_lakehouse(metadata: dict) -> tuple[str, str]:
    """
    (lakehouse name, lakehouse id) from the notebook's attached environment.

    Fabric stores this under
    `dependencies.lakehouse.{default_lakehouse, default_lakehouse_name}`.
    """
    dependencies = metadata.get("dependencies") or {}
    lakehouse = dependencies.get("lakehouse") or {}
    return (
        lakehouse.get("default_lakehouse_name") or "",
        lakehouse.get("default_lakehouse") or "",
    )


# --- PySpark ---------------------------------------------------------------

_READ_CALLS = {"table", "load"}
_HTTP_CALLS = {
    "requests.get",
    "requests.post",
    "requests.request",
    "httpx.get",
    "httpx.post",
    "urllib.request.urlopen",
}
_WRITE_CALLS = {"saveAsTable", "save", "insertInto", "toTable"}
# DataFrameWriterV2: `df.writeTo("t").append()` — the action names the mode.
_WRITE_TO_ACTIONS = {
    "append": "append",
    "overwrite": "overwrite",
    "overwritePartitions": "overwrite",
    "create": "create",
    "createOrReplace": "overwrite",
    "replace": "overwrite",
}
# spark.read.csv("Files/…") and friends: a lakehouse's raw files as a source.
_FILE_READERS = {"csv", "json", "parquet", "text", "orc", "load", "xml", "avro"}


# Candidate values an expression can take are capped: a nested dict indexed by
# two unknown keys could otherwise multiply into hundreds of phantom tables.
_MAX_CANDIDATES = 16
_VAR_PATH = re.compile(r"\$\(\s*/\*\*/(?P<lib>[^/)]+)/(?P<var>[^)]+)\)")


class _VarLib:
    """The object `notebookutils.variableLibrary.getLibrary("lib")` returns."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class _Evaluator:
    """
    Just enough constant propagation to follow how real notebooks build names.

    Metadata-driven notebooks rarely write a literal table name. They write
    `f"{lakehouse_abfss}Tables/{schema}/{table}"`, where `lakehouse_abfss` came
    from a `%run nb_config` that formatted a template with `config[env]`, and
    `env` came from a Variable Library. Each value is tracked as a short list of
    *candidates*: one when it is known, several when it depends on something
    that is not (an unknown dict key yields every value in the dict). Anything
    else is None — unknown — and the caller reports it rather than guessing.
    """

    def __init__(
        self,
        variables: dict[str, str] | None = None,
        workspace_name: str = "",
        workspace_id: str = "",
    ):
        self.consts: dict[str, list] = {}
        # The workspace the notebook runs in: `getWorkspaceName()` and the
        # runtime context answer with it (a %run notebook runs in the caller's).
        self.workspace_name = workspace_name
        self.workspace_id = workspace_id
        # name -> literal rows, for `metadata = [("a", "x"), ("b", "y")]`. Kept
        # as rows (not flattened candidates) so a loop that unpacks them binds
        # each pair together rather than every combination of the columns.
        self.rows: dict[str, list[tuple[str, ...]]] = {}
        self.variables = {k.lower(): v for k, v in (variables or {}).items()}

    # -- assignment tracking --------------------------------------------
    def rows_of(self, node) -> list[tuple[str, ...]] | None:
        """Literal rows an iterable holds, or None when any element is not literal."""
        if isinstance(node, ast.Name):
            return self.rows.get(node.id)
        if not isinstance(node, ast.List | ast.Tuple):
            return None
        rows: list[tuple[str, ...]] = []
        for element in node.elts:
            parts = element.elts if isinstance(element, ast.Tuple | ast.List) else [element]
            row = []
            for part in parts:
                values = self.strings(part)
                if not values or len(values) != 1:
                    return None
                row.append(values[0])
            rows.append(tuple(row))
        return rows

    def bind(self, target, value_node) -> None:
        if isinstance(target, ast.Name):
            rows = (
                self.rows_of(value_node)
                if isinstance(value_node, ast.List | ast.Tuple)
                else None
            )
            if rows is not None:
                self.rows[target.id] = rows
            else:
                self.rows.pop(target.id, None)
            value = self.eval(value_node)
            if value is None:
                self.consts.pop(target.id, None)
            else:
                self.consts[target.id] = value
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    self.consts.pop(element.id, None)

    def bind_loop(self, target, iter_node) -> None:
        """`for table in ["a", "b"]:` binds `table` to both candidates."""
        if not isinstance(target, ast.Name):
            return
        values = self.eval(iter_node)
        items = [v for value in (values or []) if isinstance(value, list) for v in value]
        strings = [v for v in items if isinstance(v, str)][:_MAX_CANDIDATES]
        if strings:
            self.consts[target.id] = strings
        else:
            self.consts.pop(target.id, None)

    # -- evaluation -----------------------------------------------------
    def strings(self, node) -> list[str] | None:
        values = self.eval(node)
        if not values:
            return None
        out = [v for v in values if isinstance(v, str)]
        return out or None

    def eval(self, node) -> list | None:  # noqa: PLR0911, PLR0912 — one case per node type
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return [node.value]
            if isinstance(node.value, int | float) and not isinstance(node.value, bool):
                return [str(node.value)]
            return None
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        if isinstance(node, ast.JoinedStr):
            combos = [""]
            for part in node.values:
                if isinstance(part, ast.Constant):
                    pieces = [str(part.value)]
                elif isinstance(part, ast.FormattedValue):
                    pieces = self.strings(part.value)
                    if pieces is None:
                        return None
                else:
                    return None
                combos = [c + p for c in combos for p in pieces][:_MAX_CANDIDATES]
            return combos
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.strings(node.left), self.strings(node.right)
            if left is None or right is None:
                return None
            return [a + b for a in left for b in right][:_MAX_CANDIDATES]
        if isinstance(node, ast.Dict):
            out: dict = {}
            for key, value in zip(node.keys, node.values, strict=False):
                keys = self.strings(key) if key is not None else None
                values = self.eval(value)
                if keys and len(keys) == 1 and values is not None:
                    out[keys[0]] = values
            return [out]
        if isinstance(node, ast.List | ast.Tuple):
            elements = []
            for element in node.elts:
                value = self.strings(element)
                if value:
                    elements.extend(value)
            return [elements[:_MAX_CANDIDATES]]
        if isinstance(node, ast.Subscript):
            containers = self.eval(node.value)
            if not containers:
                return None
            keys = self.strings(node.slice)
            out = []
            index = _int_index(node.slice)
            for container in containers:
                if isinstance(container, list) and index is not None:
                    try:
                        element = container[index]
                    except IndexError:
                        continue
                    if isinstance(element, str):
                        out.append(element)
                    continue
                if not isinstance(container, dict):
                    continue
                if keys is None:
                    # Unknown key: every value is a candidate.
                    for value in container.values():
                        out.extend(value)
                else:
                    for key in keys:
                        out.extend(container.get(key) or [])
            return out[:_MAX_CANDIDATES] or None
        if isinstance(node, ast.Attribute) and _dotted(node).endswith("runtime.context"):
            context = {}
            if self.workspace_name:
                context["currentWorkspaceName"] = [self.workspace_name]
            if self.workspace_id:
                context["currentWorkspaceId"] = [self.workspace_id]
            return [context] if context else None
        if isinstance(node, ast.Attribute):
            owners = self.eval(node.value)
            if owners:
                out = []
                for owner in owners:
                    if isinstance(owner, _VarLib):
                        value = self.variables.get(f"{owner.name}/{node.attr}".lower())
                        if value is not None:
                            out.append(str(value))
                return out or None
            return None
        if isinstance(node, ast.Call):
            return self._call(node)
        return None

    def _string_method(self, func: ast.Attribute, node: ast.Call) -> list | None:
        """`env.split("-")[-1]`, `name.lower()`: how config notebooks derive keys."""
        values = self.strings(func.value)
        if values is None:
            return None
        args = [self.strings(a) for a in node.args]
        if any(a is None for a in args):
            return None
        literal = [a[0] for a in args]
        out: list = []
        for value in values:
            try:
                if func.attr in ("split", "rsplit"):
                    sep = literal[0] if literal else None
                    out.append(getattr(value, func.attr)(sep))
                elif func.attr == "replace" and len(literal) >= 2:
                    out.append(value.replace(literal[0], literal[1]))
                elif func.attr == "strip":
                    out.append(value.strip(*literal[:1]))
                elif func.attr in ("lower", "upper"):
                    out.append(getattr(value, func.attr)())
                else:
                    return None
            except (TypeError, ValueError):
                return None
        return out[:_MAX_CANDIDATES] or None

    def _call(self, node: ast.Call) -> list | None:
        func = node.func
        dotted = _dotted(func)
        # notebookutils / mssparkutils Variable Library access.
        if dotted.endswith("variableLibrary.getLibrary") and node.args:
            names = self.strings(node.args[0])
            return [_VarLib(n) for n in names] if names else None
        if dotted.endswith("variableLibrary.get") and node.args:
            refs = self.strings(node.args[0])
            out = []
            for ref in refs or []:
                match = _VAR_PATH.search(ref)
                if match:
                    key = f"{match.group('lib')}/{match.group('var')}".lower()
                    if key in self.variables:
                        out.append(str(self.variables[key]))
            return out or None
        # The workspace the notebook runs in.
        if dotted.endswith("env.getWorkspaceName") or dotted.endswith(
            "env.getworkspacename"
        ):
            return [self.workspace_name] if self.workspace_name else None
        if dotted.endswith("env.getWorkspaceId"):
            return [self.workspace_id] if self.workspace_id else None
        if isinstance(func, ast.Attribute) and func.attr in _STRING_METHODS:
            return self._string_method(func, node)
        if isinstance(func, ast.Attribute) and func.attr == "format":
            templates = self.strings(func.value)
            if templates is None:
                return None
            args = [self.strings(a) for a in node.args]
            if any(a is None for a in args):
                return None
            out = []
            for template in templates:
                for combo in _product(args):
                    try:
                        out.append(template.format(*combo))
                    except (IndexError, KeyError, ValueError):
                        return None
            return out[:_MAX_CANDIDATES]
        if dotted in ("os.path.join", "posixpath.join"):
            parts = [self.strings(a) for a in node.args]
            if any(p is None for p in parts):
                return None
            return ["/".join(p.strip("/") for p in combo) for combo in _product(parts)][
                :_MAX_CANDIDATES
            ]
        return None


_STRING_METHODS = {"split", "rsplit", "lower", "upper", "strip", "replace"}


def _int_index(node) -> int | None:
    """`[-1]`, `[0]`: a literal integer index."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    ):
        return -node.operand.value
    return None


def _reads_files(call: ast.Call) -> bool:
    """`spark.read.csv(...)`, `spark.read.format(x).load(...)`: a reader chain."""
    return _in_chain(call, "read") or _in_chain(call, "readStream")


def _in_chain(node, attribute: str) -> bool:
    """Whether a method chain passes through `.attribute` anywhere before this call."""
    current = node.func if isinstance(node, ast.Call) else node
    while isinstance(current, ast.Attribute | ast.Call | ast.Subscript):
        if isinstance(current, ast.Attribute) and current.attr == attribute:
            return True
        current = current.func if isinstance(current, ast.Call) else current.value
    return False


def _own_nodes(function: ast.FunctionDef):
    """The function's own statements and expressions, not those of nested functions."""
    stack = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                stack.append(child)


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _product(lists: list[list[str]]) -> list[tuple[str, ...]]:
    combos: list[tuple[str, ...]] = [()]
    for values in lists:
        combos = [c + (v,) for c in combos for v in values][:_MAX_CANDIDATES]
    return combos


class _PySparkVisitor(ast.NodeVisitor):
    """
    Tracks DataFrame variables through assignments and records reads/writes.

    Deliberately shallow: it follows `df = spark.read.table(...)` and
    `out = df.select(...)` so a write can be attributed to the tables its chain
    read, but it does not attempt to interpret arbitrary Python. Anything it
    cannot follow becomes a warning, never a silent omission.
    """

    def __init__(
        self,
        result: ParseResult,
        cell_label: str,
        default_lh: str,
        evaluator: "_Evaluator | None" = None,
        default_lh_id: str = "",
    ):
        self.result = result
        self.cell_label = cell_label
        self.default_lh = default_lh
        # The attached lakehouse's GUID: exact where the name is not — dev and
        # prod lakehouses routinely share a name.
        self.default_lh_id = default_lh_id
        self.evaluator = evaluator or _Evaluator()
        # The notebook's own functions, inlined at each call with the arguments
        # bound — a helper that writes `f"projops.mapping_{entity}_{attr}"` is
        # only resolvable at its call sites.
        self.functions: dict[str, ast.FunctionDef] = {}
        # function name -> the tables its return value was built from, so
        # `daily = fetch(date)` carries what `fetch` read (an API, a table).
        self.returns: dict[str, list[TableRef]] = {}
        # variable name -> the expression last assigned to it, for a URL built
        # with runtime parts and passed by name: `url = f"…"; requests.get(url)`.
        self.assigned: dict[str, ast.expr] = {}
        self.called: set[str] = set()
        self.depth = 0
        # variable name -> tables that variable's chain reads from
        self.frames: dict[str, list[TableRef]] = {}
        # variable name -> column edges accumulated on that frame
        self.frame_columns: dict[str, list[ColumnEdgeRef]] = {}

    # -- helpers ---------------------------------------------------------
    def _qualify(self, name: str) -> TableRef:
        ref = parse_qualified(name)
        if not ref.lakehouse and (self.default_lh or self.default_lh_id):
            ref = TableRef(
                lakehouse=self.default_lh,
                item_id=self.default_lh_id,
                schema=ref.schema,
                table=ref.table,
            )
        return ref

    def _literal(self, node) -> str | None:
        """The one value an expression resolves to, or None when it is not one."""
        values = self.evaluator.strings(node)
        return values[0] if values and len(set(values)) == 1 else None

    def _candidates(self, node) -> list[str] | None:
        """Every value an expression can take (see _Evaluator), or None if unknown."""
        values = self.evaluator.strings(node)
        return list(dict.fromkeys(values)) if values else None

    def _path_ref(self, value: str) -> TableRef:
        """
        A path, carrying the default lakehouse for the relative forms.

        `Tables/x` and `/lakehouse/default/Tables/x` mean the notebook's
        default lakehouse; a full abfss:// or https:// URL names its own.
        """
        if value.lower().startswith(("abfss://", "https://")):
            return TableRef(path=value)
        return TableRef(path=value, lakehouse=self.default_lh, item_id=self.default_lh_id)

    def _unresolved(self, node) -> str:
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001
            return "<expression>"

    def _chain_root(self, node) -> str | None:
        """Walk a method chain back to the variable it started from."""
        current = node
        while isinstance(current, ast.Call | ast.Attribute | ast.Subscript):
            current = current.func if isinstance(current, ast.Call) else current.value
        return current.id if isinstance(current, ast.Name) else None

    def _reads_of(self, node) -> list[TableRef]:
        """Tables feeding an expression: its own reads plus any frame it uses."""
        tables: list[TableRef] = []
        for name in {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}:
            tables.extend(self.frames.get(name, []))
        # A call to one of the notebook's own functions reads what it returns.
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                tables.extend(self.returns.get(call.func.id, []))
        tables.extend(self._direct_reads(node))
        deduped: dict[str, TableRef] = {}
        for table in tables:
            deduped.setdefault(table.key(), table)
        return list(deduped.values())

    def _direct_reads(self, node) -> list[TableRef]:
        """`spark.read.table(...)`, `spark.table(...)`, `DeltaTable.forName(...)`, HTTP."""
        out: list[TableRef] = []
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            attribute = call.func.attr
            if _dotted(call.func) in _HTTP_CALLS:
                out.extend(self._http_reads(call))
                continue

            if attribute in ("table", "forName", "forPath"):
                # DeltaTable.forName(spark, "x") — the name is the 2nd argument.
                args = [a for a in call.args if not _is_spark(a)]
                if not args:
                    continue
                values = self._candidates(args[0])
                if values is None:
                    self.result.warn(
                        f"{self.cell_label}: dynamic table name {self._unresolved(args[0])}"
                    )
                    out.append(TableRef(dynamic_expr=self._unresolved(args[0])))
                    continue
                for value in values:
                    out.append(
                        self._path_ref(value)
                        if attribute == "forPath"
                        else self._qualify(value)
                    )

            elif attribute in _FILE_READERS and call.args and _reads_files(call):
                values = self._candidates(call.args[0])
                if values is None:
                    if attribute == "load":
                        out.append(TableRef(dynamic_expr=self._unresolved(call.args[0])))
                    continue
                for value in values:
                    if "Tables" in value:
                        out.append(self._path_ref(value))
                    elif "Files" in value:
                        out.append(self._files_ref(value))
        return out

    def _files_ref(self, value: str) -> TableRef:
        """
        Raw files in a lakehouse's Files area (`Files/raw/orders.csv`), as a
        source: where a bronze table's rows came from before they were a table.
        """
        path = value.split("Files/", 1)[1] if "Files/" in value else value
        where = ""
        if value.lower().startswith("abfss://"):
            parsed = urlparse(value)
            where = (parsed.path.strip("/").split("/", 1) or [""])[0]
        where = where or self.default_lh or "lakehouse"
        return TableRef(
            table=f"Files/{path}",
            is_external=True,
            connection={"kind": "files", "url": f"{where} (Files)"},
        )

    def _http_reads(self, call: ast.Call) -> list[TableRef]:
        """
        An API call as an external source: `requests.get(f"{org}/api/data/…")`.

        Keyed on scheme://host plus the first two path segments, so one
        Dataverse org read through a hundred entity URLs is one source, not a
        hundred.
        """
        url_node = next((k.value for k in call.keywords if k.arg == "url"), None)
        if url_node is None and call.args:
            url_node = (
                call.args[1]
                if _dotted(call.func).endswith(".request") and len(call.args) > 1
                else call.args[0]
            )
        values = self._candidates(url_node) if url_node is not None else None
        if isinstance(url_node, ast.Name) and url_node.id in self.assigned:
            url_node = self.assigned[url_node.id] if values is None else url_node
        if values is None and isinstance(url_node, ast.JoinedStr):
            # `f"https://api.x.io/v1/games?cursor={cursor}"`: the parts known
            # before the first runtime value still name the API and endpoint.
            prefix = ""
            for part in url_node.values:
                if not isinstance(part, ast.Constant):
                    break
                prefix += str(part.value)
            if "://" in prefix:
                values = [prefix]
        refs = []
        for value in values or []:
            parsed = urlparse(value)
            if not parsed.scheme or not parsed.netloc:
                continue
            segments = [p for p in parsed.path.split("/") if p][:2]
            refs.append(
                TableRef(
                    table="/" + "/".join(segments) if segments else parsed.netloc,
                    is_external=True,
                    connection={"kind": "web", "url": f"{parsed.scheme}://{parsed.netloc}"},
                )
            )
        if not refs:
            refs.append(
                TableRef(
                    table="(dynamic URL)", is_external=True, connection={"kind": "web"}
                )
            )
        return list({r.key() + str(r.connection): r for r in refs}.values())

    # -- visits ----------------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        # The value first: a call to one of the notebook's functions is inlined
        # here, which records what it returns before the target inherits it.
        self.visit(node.value)
        for target in node.targets:
            self.evaluator.bind(target, node.value)
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for name in targets:
            self.assigned[name] = node.value
        if targets:
            reads = self._reads_of(node.value)
            columns = self._select_columns(node.value)
            for name in targets:
                self.frames[name] = reads
                self.frame_columns[name] = columns

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Not visited here: its body means something only with arguments, so it
        # is walked at each call (and once, unbound, if never called).
        self.functions[node.name] = node

    def visit_For(self, node: ast.For) -> None:
        rows = self.evaluator.rows_of(node.iter)
        if rows is not None and 0 < len(rows) <= 64:
            # A loop over literal rows is unrolled: each iteration binds its own
            # values, so `for table, column in metadata:` yields exactly the
            # pairs listed, not every combination of them.
            targets = (
                node.target.elts
                if isinstance(node.target, ast.Tuple | ast.List)
                else [node.target]
            )
            for row in rows:
                for index, target in enumerate(targets):
                    if isinstance(target, ast.Name):
                        if index < len(row):
                            self.evaluator.consts[target.id] = [row[index]]
                        else:
                            self.evaluator.consts.pop(target.id, None)
                for statement in node.body:
                    self.visit(statement)
            for statement in node.orelse:
                self.visit(statement)
            return
        self.evaluator.bind_loop(node.target, node.iter)
        if isinstance(node.target, ast.Name):
            # `for o in options:` — o carries whatever options was read from.
            self.frames[node.target.id] = self._reads_of(node.iter)
        self.generic_visit(node)

    def _inline(self, node: ast.Call, function: ast.FunctionDef) -> None:
        params = [a.arg for a in function.args.args]
        saved_consts = dict(self.evaluator.consts)
        saved_rows = dict(self.evaluator.rows)
        saved_frames = dict(self.frames)
        bindings = list(zip(params, node.args, strict=False)) + [
            (k.arg, k.value) for k in node.keywords if k.arg in params
        ]
        # Evaluate every argument in the caller's scope first: `load(entity)`
        # passing a variable named like the parameter must not see it cleared.
        evaluated = [
            (param, self.evaluator.eval(argument), self._reads_of(argument))
            for param, argument in bindings
        ]
        for param in params:
            self.evaluator.consts.pop(param, None)
            self.frames.pop(param, None)
        for param, value, reads in evaluated:
            if value is not None:
                self.evaluator.consts[param] = value
            self.frames[param] = reads
        self.depth += 1
        try:
            for statement in function.body:
                self.visit(statement)
            self._record_returns(function)
        finally:
            self.depth -= 1
            self.evaluator.consts = saved_consts
            self.evaluator.rows = saved_rows
            self.frames = saved_frames

    def _record_returns(self, function: ast.FunctionDef) -> None:
        """What the function's `return` values were read from, in its own scope."""
        merged = {r.key(): r for r in self.returns.get(function.name, [])}
        for node in _own_nodes(function):
            if isinstance(node, ast.Return) and node.value is not None:
                for ref in self._reads_of(node.value):
                    merged.setdefault(ref.key(), ref)
        self.returns[function.name] = list(merged.values())

    def visit_uncalled(self) -> None:
        """Walk functions nobody called, so their literal writes still count."""
        for name, function in list(self.functions.items()):
            if name in self.called:
                continue
            self.called.add(name)
            for statement in function.body:
                self.visit(statement)
            self._record_returns(function)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in self.functions
            and self.depth < 3
        ):
            self.called.add(node.func.id)
            self._inline(node, self.functions[node.func.id])
        if isinstance(node.func, ast.Attribute):
            attribute = node.func.attr
            if attribute in ("append", "extend") and isinstance(node.func.value, ast.Name):
                # `rows.append(Row(...))` — the list now carries what was appended.
                name = node.func.value.id
                merged = {r.key(): r for r in self.frames.get(name, [])}
                for argument in node.args:
                    for ref in self._reads_of(argument):
                        merged.setdefault(ref.key(), ref)
                self.frames[name] = list(merged.values())

            writer = node.func.value
            if (
                attribute in _WRITE_TO_ACTIONS
                and isinstance(writer, ast.Call)
                and isinstance(writer.func, ast.Attribute)
                and writer.func.attr == "writeTo"
            ):
                self._record_write(writer, "writeTo", mode=_WRITE_TO_ACTIONS[attribute])
            elif attribute == "start" and node.args and _in_chain(node, "writeStream"):
                # A streaming write to a path: writeStream...start("Tables/x").
                self._record_write(node, "save", default="append")
            elif attribute in _WRITE_CALLS:
                # toTable is a stream's sink: rows arrive by appending.
                self._record_write(
                    node, attribute, default="append" if attribute == "toTable" else ""
                )
            elif attribute == "sql":
                self._record_spark_sql(node)
            elif attribute == "merge":
                self._record_merge(node)
            elif attribute == "run":
                self._record_run(node)

        self.generic_visit(node)

    # -- recorders -------------------------------------------------------
    def _record_write(
        self, node: ast.Call, attribute: str, mode: str | None = None, default: str = ""
    ) -> None:
        """
        `mode` is a mode the call itself states (`writeTo(t).append()`), which
        wins; `default` applies only when the chain has no `.mode(...)`.
        """
        if not node.args:
            return
        kwargs = {"mode": mode, "default": default}
        values = self._candidates(node.args[0])
        if values is None:
            expression = self._unresolved(node.args[0])
            self.result.warn(f"{self.cell_label}: dynamic write target {expression}")
            self._record_one_write(
                node, attribute, TableRef(dynamic_expr=expression), **kwargs
            )
            return
        if len(values) > 1:
            # Several possible targets (a dict indexed by an unknown key, a loop
            # over a list): each is a candidate, none is claimed with certainty.
            self.result.warn(
                f"{self.cell_label}: write target resolves to {len(values)} candidates"
            )
        for value in values:
            target = self._path_ref(value) if attribute == "save" else self._qualify(value)
            self._record_one_write(node, attribute, target, low=len(values) > 1, **kwargs)

    def _record_one_write(
        self,
        node: ast.Call,
        attribute: str,
        target: TableRef,
        *,
        low: bool = False,
        mode: str | None = None,
        default: str = "",
    ) -> None:
        mode = (
            mode
            or self._write_mode(node)
            or default
            or ("append" if attribute == "insertInto" else "overwrite")
        )
        root = self._chain_root(node)
        reads = self.frames.get(root, []) if root else []
        if not reads:
            reads = self._reads_of(node)

        columns = list(self.frame_columns.get(root, [])) if root else []
        retargeted = [
            ColumnEdgeRef(
                source=edge.source,
                target=ColumnRef(table=target, column=edge.target.column),
                transform=edge.transform,
                confidence=edge.confidence,
            )
            for edge in columns
        ]

        self.result.writes.append(
            Write(
                table=target,
                mode=mode,
                via_ref=self.cell_label,
                reads=reads,
                column_edges=retargeted,
                confidence=CONF_LOW if low else CONF_MEDIUM,
                # Columns the chain did not project or rename keep their names;
                # the resolver pairs them against the target's real schema.
                passthrough=True,
            )
        )
        self.result.column_edges.extend(retargeted)

    def _write_mode(self, node: ast.Call) -> str:
        """Find `.mode("overwrite")` anywhere in the chain before the write."""
        current = node.func
        while isinstance(current, ast.Attribute | ast.Call):
            if isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
                if current.func.attr == "mode" and current.args:
                    literal = self._literal(current.args[0])
                    if literal:
                        return literal.lower()
            current = current.func if isinstance(current, ast.Call) else current.value
        return ""

    def _record_spark_sql(self, node: ast.Call) -> None:
        if not node.args:
            return
        values = self._candidates(node.args[0])
        if values is None:
            # An f-string SQL body whose variables we cannot follow. Record the
            # fact rather than pretending the notebook reads nothing.
            expression = self._unresolved(node.args[0])
            self.result.warn(f"{self.cell_label}: dynamic spark.sql({expression[:80]})")
            self.result.reads.append(TableRef(dynamic_expr=expression))
            self.result.confidence = CONF_LOW
            return
        for value in values[:4]:
            nested = parse_sql(value, via_ref=self.cell_label)
            _qualify_result(nested, self.default_lh, self.default_lh_id)
            self.result.merge(nested)

    def _record_merge(self, node: ast.Call) -> None:
        """
        `DeltaTable.forName(...).alias("t").merge(source, condition)`.

        The merge target is the forName/forPath at the root of the chain; the
        source is the first argument.
        """
        root_reads = self._direct_reads(node.func)
        if not root_reads:
            return
        target = root_reads[0]
        source_reads = self._reads_of(node.args[0]) if node.args else []
        root = self._chain_root(node.args[0]) if node.args else None
        columns = list(self.frame_columns.get(root, [])) if root else []
        retargeted = [
            ColumnEdgeRef(
                source=edge.source,
                target=ColumnRef(table=target, column=edge.target.column),
                transform=edge.transform,
                confidence=edge.confidence,
            )
            for edge in columns
        ]
        self.result.writes.append(
            Write(
                table=target,
                mode="merge",
                via_ref=self.cell_label,
                reads=source_reads,
                column_edges=retargeted,
                confidence=CONF_MEDIUM,
                passthrough=True,  # whenMatchedUpdateAll / whenNotMatchedInsertAll
            )
        )
        self.result.column_edges.extend(retargeted)

    def _record_run(self, node: ast.Call) -> None:
        """`notebookutils.notebook.run("nb_shared")` — an item dependency."""
        if not node.args:
            return
        literal = self._literal(node.args[0])
        if literal:
            self.result.invokes.append(literal)

    # -- column extraction ----------------------------------------------
    def _select_columns(self, node) -> list[ColumnEdgeRef]:
        """
        Best-effort column lineage from `select(...)`, `withColumn`,
        `withColumnRenamed`.

        The source table is only known when the chain reads exactly one — with a
        join, attributing a column to one side would be a guess, so those come
        out at table level with the expression recorded and confidence low.
        """
        edges: list[ColumnEdgeRef] = []
        reads = self._reads_of(node)
        single = reads[0] if len(reads) == 1 else None
        confidence = CONF_MEDIUM if single else CONF_LOW

        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            attribute = call.func.attr

            if attribute == "select":
                for argument in call.args:
                    edges.extend(self._projection(argument, single, confidence))
            elif attribute == "withColumn" and len(call.args) >= 2:
                name = self._literal(call.args[0])
                if name:
                    edges.extend(
                        self._from_expression(call.args[1], name, single, confidence)
                    )
            elif attribute == "withColumnRenamed" and len(call.args) >= 2:
                old, new = self._literal(call.args[0]), self._literal(call.args[1])
                if old and new:
                    edges.append(
                        ColumnEdgeRef(
                            source=ColumnRef(table=single or TableRef(), column=old),
                            target=ColumnRef(table=TableRef(), column=new),
                            transform="rename",
                            confidence=confidence,
                        )
                    )
        return edges

    def _projection(self, node, single, confidence) -> list[ColumnEdgeRef]:
        """One argument of `.select(...)`, which may carry `.alias("x")`."""
        alias = None
        current = node
        while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
            if current.func.attr == "alias" and current.args:
                alias = self._literal(current.args[0]) or alias
            current = current.func.value

        columns = _column_literals(node)
        if not columns:
            return []
        target_name = alias or columns[0]
        expression = _expression_text(node)
        # `col("X").alias("y")` is a rename; `upper(trim(col("X"))).alias("y")`
        # is a transformation and has to show its expression, or the trace view
        # claims the value passed through untouched.
        bare = _is_bare_column(expression, columns)
        if bare:
            transform = "rename" if alias and alias != columns[0] else "passthrough"
        else:
            transform = expression
        return [
            ColumnEdgeRef(
                source=ColumnRef(table=single or TableRef(), column=column),
                target=ColumnRef(table=TableRef(), column=target_name),
                transform=transform,
                confidence=confidence,
            )
            for column in columns
        ]

    def _from_expression(self, node, target_name, single, confidence):
        columns = _column_literals(node)
        transform = _expression_text(node)
        if not columns:
            # e.g. withColumn("_loaded_at", current_timestamp()) — no input
            # column, but the derivation is still worth showing.
            return [
                ColumnEdgeRef(
                    source=ColumnRef(table=single or TableRef(), column=""),
                    target=ColumnRef(table=TableRef(), column=target_name),
                    transform=transform,
                    confidence=CONF_LOW,
                )
            ]
        return [
            ColumnEdgeRef(
                source=ColumnRef(table=single or TableRef(), column=column),
                target=ColumnRef(table=TableRef(), column=target_name),
                transform=transform,
                confidence=confidence,
            )
            for column in columns
        ]


def _is_bare_column(expression: str, columns: list[str]) -> bool:
    """
    True when the expression is nothing but a single column reference.

    Compared textually against the forms PySpark uses for a bare reference, so
    anything wrapped in a function falls through to showing its real expression.
    """
    if len(columns) != 1:
        return False
    name = columns[0]
    stripped = expression.strip()
    return stripped in {
        name,
        f"col('{name}')",
        f'col("{name}")',
        f"column('{name}')",
        f'column("{name}")',
        f"df['{name}']",
        f'df["{name}"]',
    }


def _is_spark(node) -> bool:
    return isinstance(node, ast.Name) and node.id == "spark"


def _column_literals(node) -> list[str]:
    """
    Column names referenced by an expression: `col("X")`, `"X"`, `df["X"]`.

    Returns them in source order and deduplicated, so
    `col("A") - col("B")` yields ["A", "B"].
    """
    out: list[str] = []

    def add(value: str) -> None:
        # `col("c.*")` is every column of alias c — not a column; the write is
        # a pass-through for those and the resolver pairs them by name.
        if not value or value == "*" or value.endswith(".*"):
            return
        # `col("b.id")` qualifies by a join alias; Spark names the output `id`.
        if "." in value and value.count(".") == 1 and not value.startswith("`"):
            value = value.split(".", 1)[1]
        if value not in out:
            out.append(value)

    for inner in ast.walk(node):
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
            if inner.func.id in ("col", "column") and inner.args:
                if isinstance(inner.args[0], ast.Constant) and isinstance(
                    inner.args[0].value, str
                ):
                    add(inner.args[0].value)
        elif isinstance(inner, ast.Subscript):
            if isinstance(inner.slice, ast.Constant) and isinstance(inner.slice.value, str):
                add(inner.slice.value)

    if not out and isinstance(node, ast.Constant) and isinstance(node.value, str):
        add(node.value)
    return out


def _expression_text(node) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""
    # `.alias("x")` is the naming, not the transformation.
    return re.sub(r"\.alias\(['\"][^'\"]*['\"]\)\s*$", "", text).strip()


# --- entry point -----------------------------------------------------------
def parse_notebook(
    source: str,
    *,
    default_lakehouse_name: str = "",
    run_sources: dict[str, str] | None = None,
    variables: dict[str, str] | None = None,
    parameters: dict[str, str] | None = None,
    workspace_name: str = "",
    workspace_id: str = "",
) -> ParseResult:
    """
    Parse a Fabric notebook into reads, writes, column edges and invocations.

    `default_lakehouse_name` overrides what the META block says, for the case
    where a pipeline attaches a different lakehouse at run time.
    `run_sources` maps notebook name -> source for the notebooks this one may
    `%run`: their top-level assignments run first in the same session, exactly
    as Fabric executes them, so a config notebook's paths become known here.
    `variables` maps `library/variable` (lowercase) to the workspace's active
    Variable Library value.
    `parameters` are the values a pipeline's Notebook activity passes: they
    replace the parameter cell's defaults once that cell has run, as Fabric
    does by inserting a cell after it.
    `workspace_name` / `workspace_id` are the notebook's own workspace, which
    `getWorkspaceName()` and the runtime context return at run time — how
    config notebooks pick dev / prod.
    """
    result = ParseResult()
    run_sources = {k.lower(): v for k, v in (run_sources or {}).items()}
    cells, metadata = split_cells(source)
    meta_lakehouse, meta_lakehouse_id = default_lakehouse(metadata)
    lakehouse = default_lakehouse_name or meta_lakehouse
    lakehouse_id = "" if default_lakehouse_name else meta_lakehouse_id

    if not lakehouse:
        result.warn(
            "No default lakehouse in the notebook metadata — unqualified table "
            "names could not be resolved."
        )

    # One visitor for the whole notebook: `df = spark.read.table(...)` in one
    # cell and `df.write.saveAsTable(...)` in another is the normal shape, and a
    # per-cell visitor loses the connection between them — every PySpark write
    # then reports reading nothing.
    evaluator = _Evaluator(
        variables, workspace_name=workspace_name, workspace_id=workspace_id
    )
    visitor = _PySparkVisitor(result, "", lakehouse, evaluator, lakehouse_id)

    for cell in cells:
        text = cell.text

        for match in _MAGIC_RUN.finditer(text):
            target = match.group("target").strip("\"'")
            result.invokes.append(target)
            _absorb_run(run_sources, target, evaluator, depth=0)

        if _MAGIC_SQL.search(text):
            body = _MAGIC_SQL.sub("", text, count=1)
            nested = parse_sql(body, via_ref=cell.label)
            _qualify_result(nested, lakehouse, lakehouse_id)
            result.merge(nested)
            continue

        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            # A cell with a magic we do not handle (%%configure, %pip) is not a
            # failure worth shouting about, but an unparseable code cell is.
            if not text.lstrip().startswith("%"):
                result.warn(f"{cell.label}: Python did not parse ({e.msg})")
                result.confidence = CONF_MEDIUM
            continue

        visitor.cell_label = cell.label
        visitor.visit(tree)
        if cell.kind == "PARAMETERS" and parameters:
            for name, value in parameters.items():
                evaluator.consts[name] = [value]

    visitor.visit_uncalled()
    _attach_column_sources(result)
    return result


def _absorb_run(
    run_sources: dict[str, str], target: str, evaluator: _Evaluator, *, depth: int
) -> None:
    """
    Apply a `%run` notebook's top-level assignments to the evaluator.

    Only assignments: its reads and writes belong to that notebook and are
    parsed there. Nested `%run` is followed a few levels deep, which covers the
    config-of-a-config pattern without risking a cycle.
    """
    name = target.rsplit("/", 1)[-1].lower()
    source = run_sources.get(name)
    if not source or depth > 3:
        return
    cells, _ = split_cells(source)
    for cell in cells:
        for match in _MAGIC_RUN.finditer(cell.text):
            _absorb_run(
                run_sources, match.group("target").strip("\"'"), evaluator, depth=depth + 1
            )
        try:
            tree = ast.parse(cell.text)
        except SyntaxError:
            continue
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                for assigned in statement.targets:
                    evaluator.bind(assigned, statement.value)


def _qualify_result(result: ParseResult, lakehouse: str, lakehouse_id: str = "") -> None:
    """Apply the default lakehouse to unqualified refs from a %%sql cell."""
    if not lakehouse and not lakehouse_id:
        return

    def fix(ref: TableRef) -> TableRef:
        if (
            ref.table
            and not ref.lakehouse
            and not ref.path
            and not ref.dynamic_expr
            and not ref.is_external
        ):
            return TableRef(
                lakehouse=lakehouse,
                item_id=lakehouse_id,
                schema=ref.schema,
                table=ref.table,
            )
        return ref

    result.reads[:] = [fix(r) for r in result.reads]
    for write in result.writes:
        write.table = fix(write.table)
        write.reads[:] = [fix(r) for r in write.reads]
        write.column_edges[:] = [
            ColumnEdgeRef(
                source=ColumnRef(table=fix(e.source.table), column=e.source.column),
                target=ColumnRef(table=fix(e.target.table), column=e.target.column),
                transform=e.transform,
                confidence=e.confidence,
            )
            for e in write.column_edges
        ]
    result.column_edges[:] = [
        ColumnEdgeRef(
            source=ColumnRef(table=fix(e.source.table), column=e.source.column),
            target=ColumnRef(table=fix(e.target.table), column=e.target.column),
            transform=e.transform,
            confidence=e.confidence,
        )
        for e in result.column_edges
    ]


def _attach_column_sources(result: ParseResult) -> None:
    """
    Fill in a column edge's source table when the PySpark visitor left it blank.

    The visitor builds edges while walking a chain, before it knows which write
    consumes them; a single-source write lets us attribute them afterwards.
    Edges still lacking a source table are dropped — an edge from nowhere would
    render as a floating card in the trace view.
    """
    for write in result.writes:
        sources = [r for r in write.reads if r.table]
        kept = []
        for edge in write.column_edges:
            if not edge.source.column:
                continue
            if edge.source.table.table:
                kept.append(edge)
                continue
            if not sources:
                continue
            if len(sources) == 1:
                kept.append(
                    ColumnEdgeRef(
                        source=ColumnRef(table=sources[0], column=edge.source.column),
                        target=edge.target,
                        transform=edge.transform,
                        confidence=edge.confidence,
                    )
                )
                continue
            # A join: the parser cannot know which side a column came from
            # without the schema. Emit one CANDIDATE per source at low
            # confidence; the resolver drops the ones whose source table has no
            # such column, using the schema the Delta collector already holds.
            # Guessing one side here would be wrong half the time, and emitting
            # nothing would lose the column lineage entirely.
            for source in sources:
                kept.append(
                    ColumnEdgeRef(
                        source=ColumnRef(table=source, column=edge.source.column),
                        target=edge.target,
                        transform=edge.transform,
                        confidence=CONF_LOW,
                    )
                )
        write.column_edges[:] = kept

    result.column_edges[:] = [e for w in result.writes for e in w.column_edges] + [
        e for e in result.column_edges if e.source.table.table and e.target.table.table
    ]
    # Deduplicate: the same edge can arrive from both the write and the
    # top-level list.
    seen = set()
    unique = []
    for edge in result.column_edges:
        key = (
            edge.source.table.key(),
            edge.source.column,
            edge.target.table.key(),
            edge.target.column,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    result.column_edges[:] = unique
