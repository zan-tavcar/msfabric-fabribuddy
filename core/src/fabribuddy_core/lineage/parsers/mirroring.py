"""
Mirrored database (`mirroring.json`) and Variable Library definitions.

Mirroring: the definition names the source system and, when not mirroring the
whole database, the tables (`mountedTables`). Shape per Microsoft Learn,
"Fabric Mirroring Public REST API":

    {"properties": {
        "source": {"type": "AzureSqlDatabase",
                   "typeProperties": {"connection": "<id>", "database": "sales"}},
        "target": {"type": "MountedRelationalDatabase",
                   "typeProperties": {"defaultSchema": "dbo", "format": "Delta"}},
        "mountedTables": [{"source": {"typeProperties":
                   {"schemaName": "dbo", "tableName": "orders"}}}]}}

Each mirrored table is written by the mirroring engine from the same-named
source table: an edge from an external node to the mirrored table, declared by
the definition rather than inferred.
"""

import json

from .base import CONF_HIGH, ParseResult, TableRef, Write


def parse_mirroring(
    text: str,
    *,
    workspace_guid: str,
    item_guid: str,
    tables: list[tuple[str, str]] | None = None,
) -> ParseResult:
    """
    `tables` are the (schema, table) pairs the item actually holds in OneLake.
    They stand in for `mountedTables` when the whole database is mirrored,
    which the definition expresses by omitting the list.
    """
    result = ParseResult()
    try:
        definition = json.loads(text or "{}")
    except json.JSONDecodeError:
        result.warn("mirroring.json is not valid JSON.")
        return result

    properties = definition.get("properties") or {}
    source = properties.get("source") or {}
    type_properties = source.get("typeProperties") or {}
    connection = {
        k: v
        for k, v in {
            "kind": source.get("type") or "mirroring",
            "connection": type_properties.get("connection") or "",
            "database": type_properties.get("database") or "",
        }.items()
        if v
    }

    mounted = [
        (
            str((m.get("source") or {}).get("typeProperties", {}).get("schemaName") or ""),
            str((m.get("source") or {}).get("typeProperties", {}).get("tableName") or ""),
        )
        for m in properties.get("mountedTables") or []
        if isinstance(m, dict)
    ]
    pairs = [p for p in mounted if p[1]] or list(tables or [])
    if not pairs:
        result.warn("Mirrored database: no mounted tables and none found in OneLake yet.")
        return result

    for schema, table in pairs:
        external = TableRef(
            table=f"{schema}.{table}" if schema else table,
            is_external=True,
            connection=connection,
        )
        result.writes.append(
            Write(
                table=TableRef(
                    workspace_id=workspace_guid,
                    item_id=item_guid,
                    schema=schema,
                    table=table,
                ),
                mode="mirror",
                via_ref="mirroring",
                reads=[external],
                confidence=CONF_HIGH,
            )
        )
        result.reads.append(external)
    return result


def variable_values(
    parts: dict[str, str], library: str, active_value_set: str
) -> dict[str, str]:
    """
    `library/variable` (lowercase) -> the value in effect.

    Defaults come from `variables.json`; the active value set's file under
    `valueSets/` overrides them (Microsoft Learn, "Variable library definition").
    """
    values: dict[str, str] = {}
    for path, text in parts.items():
        if path.lower().endswith("variables.json"):
            try:
                for variable in (json.loads(text) or {}).get("variables") or []:
                    if isinstance(variable, dict) and variable.get("name"):
                        values[variable["name"]] = _scalar(variable.get("value"))
            except json.JSONDecodeError:
                continue
    if active_value_set:
        for path, text in parts.items():
            lowered = path.lower()
            if "valueset" not in lowered or not lowered.endswith(".json"):
                continue
            try:
                value_set = json.loads(text) or {}
            except json.JSONDecodeError:
                continue
            name = value_set.get("name") or path.rsplit("/", 1)[-1][: -len(".json")]
            if name.lower() != active_value_set.lower():
                continue
            for override in value_set.get("variableOverrides") or []:
                if isinstance(override, dict) and override.get("name"):
                    values[override["name"]] = _scalar(override.get("value"))
    return {f"{library}/{k}".lower(): v for k, v in values.items()}


def variable_table(parts: dict[str, str], active_value_set: str) -> list[dict]:
    """
    Every variable for display: name, type, note, default, and the value the
    active value set gives it (the default when it does not override it).
    """
    rows: dict[str, dict] = {}
    for path, text in parts.items():
        if not path.lower().endswith("variables.json"):
            continue
        try:
            variables = (json.loads(text) or {}).get("variables") or []
        except json.JSONDecodeError:
            continue
        for variable in variables:
            if isinstance(variable, dict) and variable.get("name"):
                default = _scalar(variable.get("value"))
                rows[variable["name"]] = {
                    "name": variable["name"],
                    "type": str(variable.get("type") or ""),
                    "note": str(variable.get("note") or ""),
                    "default": default,
                    "active": default,
                    "overridden": False,
                }
    overrides = variable_values(parts, "x", active_value_set) if active_value_set else {}
    for row in rows.values():
        value = overrides.get(f"x/{row['name']}".lower())
        if value is not None and value != row["default"]:
            row["active"], row["overridden"] = value, True
    return sorted(rows.values(), key=lambda r: r["name"].lower())


def _scalar(value) -> str:
    if isinstance(value, dict):
        # ItemReference variables: {"workspaceId": …, "itemId": …}
        return str(value.get("itemId") or value.get("workspaceId") or "")
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)
