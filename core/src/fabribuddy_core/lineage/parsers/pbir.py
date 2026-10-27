"""
Report parser (PBIR, `definition/**/visual.json`).

Each visual becomes a node, and each field it projects becomes a column edge
from the model to the visual. That is the last hop of the chain the whole
product exists to show: "this number on this chart comes from that column in
that lakehouse table".

A projection is either a Column or a Measure:

    {"field": {"Column":  {"Expression": {"SourceRef": {"Entity": "Customer"}},
                           "Property": "Country"}}}
    {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                           "Property": "Net Sales EUR"}}}

Older exports put Entity directly on the Column object rather than under
Expression.SourceRef, so both shapes are read.
"""

import json
import logging
import re

from .base import (
    CONF_HIGH,
    ParseResult,
    TableRef,
    Write,
)

log = logging.getLogger("lineage.parse.pbir")


class Visual:
    def __init__(self, visual_id: str, visual_type: str, title: str, page: str):
        self.id = visual_id
        self.visual_type = visual_type
        self.title = title
        self.page = page
        self.fields: list[dict] = []


def _entity_and_property(field: dict) -> tuple[str, str, bool] | None:
    """(entity, property, is_measure) from one projection's field."""
    for key, is_measure in (("Column", False), ("Measure", True), ("Aggregation", True)):
        inner = field.get(key)
        if not isinstance(inner, dict):
            continue
        if key == "Aggregation":
            # An implicit aggregation wraps a Column.
            nested = (inner.get("Expression") or {}).get("Column")
            if isinstance(nested, dict):
                inner = nested
                is_measure = False
            else:
                continue
        entity = (
            ((inner.get("Expression") or {}).get("SourceRef") or {}).get("Entity")
            or inner.get("Entity")
            or ""
        )
        prop = inner.get("Property") or ""
        if prop:
            return str(entity), str(prop), is_measure
    return None


def _projections(container: dict) -> list[dict]:
    """
    Every projection in a visual, from wherever the format put it.

    PBIR nests these under `visual.query.queryState.<role>.projections`, and
    some exports use `visual.query.projections` or a `columnProperties` list.
    Walking generically is more robust than pinning one path.
    """
    out: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for entry in node:
                walk(entry)
            return
        if not isinstance(node, dict):
            return
        projections = node.get("projections")
        if isinstance(projections, list):
            for projection in projections:
                if isinstance(projection, dict):
                    out.append(projection)
        for value in node.values():
            if isinstance(value, dict | list):
                walk(value)

    walk(container)
    return out


def parse_visual(content: dict, *, page: str = "") -> Visual | None:
    """One visual.json -> a Visual with its projected fields."""
    if not isinstance(content, dict):
        return None
    visual_block = content.get("visual") or content
    visual_type = str(visual_block.get("visualType") or "")
    visual_id = str(content.get("name") or visual_block.get("name") or "")
    title = _title(visual_block) or visual_type or visual_id

    visual = Visual(visual_id, visual_type, title, page)
    for projection in _projections(visual_block):
        field = projection.get("field")
        if not isinstance(field, dict):
            continue
        parsed = _entity_and_property(field)
        if parsed is None:
            continue
        entity, prop, is_measure = parsed
        visual.fields.append({"entity": entity, "property": prop, "is_measure": is_measure})
    return visual


def _title(visual_block: dict) -> str:
    """
    The visual's title, when it has an explicit one.

    PBIR wraps it several objects deep in a properties/expr/Literal structure,
    and a visual with no title falls back to its type — which is why the
    mockup's "Net sales by country" needs this and not just visualType.
    """
    objects = (visual_block.get("visualContainerObjects") or {}).get("title") or []
    for entry in objects:
        properties = (entry or {}).get("properties") or {}
        text = (properties.get("text") or {}).get("expr") or {}
        literal = (text.get("Literal") or {}).get("Value")
        if isinstance(literal, str):
            return literal.strip().strip("'")
    return ""


_MODEL_ID = re.compile(r"semanticmodelid=(?P<id>[0-9a-fA-F-]{36})", re.IGNORECASE)


def model_reference(definition_pbir: str) -> tuple[str, str]:
    """
    (semantic model GUID, semantic model name) a report is bound to.

    From `definition.pbir` -> `datasetReference`: `byConnection` carries the
    model id in its connection string (`…;semanticmodelid=<guid>`); `byPath`
    points at a sibling `<Name>.SemanticModel` folder, i.e. a model in the same
    workspace, known by name.
    """
    try:
        reference = (json.loads(definition_pbir or "{}") or {}).get(
            "datasetReference"
        ) or {}
    except json.JSONDecodeError:
        return "", ""
    connection = (reference.get("byConnection") or {}).get("connectionString") or ""
    match = _MODEL_ID.search(str(connection))
    if match:
        return match.group("id").lower(), ""
    for key in ("pbiModelDatabaseName", "semanticModelId"):
        value = (reference.get("byConnection") or {}).get(key)
        if value:
            return str(value).lower(), ""
    path = str((reference.get("byPath") or {}).get("path") or "")
    if path:
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return "", name.rsplit(".SemanticModel", 1)[0]
    return "", ""


def parse_report(
    visuals: list[tuple[str, dict]],
    *,
    model_name: str = "",
    model_item_id: str = "",
    report_item_id: str = "",
) -> tuple[ParseResult, list[Visual]]:
    """
    A report -> one write: the report, reading every model table its visuals use.

    Report-level on purpose. What matters downstream of a semantic model is
    which report depends on which of its tables; per-visual nodes and
    field-to-visual column edges multiplied the Report lane into thousands of
    boxes without answering anything more useful. The visuals are still parsed
    (and returned) so the report's table set is exact rather than "the whole
    model".
    """
    result = ParseResult()
    parsed: list[Visual] = []
    sources: dict[str, TableRef] = {}

    for page, content in visuals:
        visual = parse_visual(content, page=page)
        if visual is None:
            continue
        parsed.append(visual)
        for field in visual.fields:
            source = (
                TableRef(item_id=model_item_id, table=field["entity"])
                if model_item_id
                else TableRef(lakehouse=model_name, table=field["entity"])
            )
            sources.setdefault(field["entity"].lower(), source)

    if parsed and not sources:
        result.warn("Report: no model fields recognised in any visual.")
    if sources:
        target = (
            TableRef(item_id=report_item_id, table="report")
            if report_item_id
            else TableRef(table="report")
        )
        result.writes.append(
            Write(
                table=target,
                mode="report",
                via_ref=f"{len(parsed)} visuals",
                reads=list(sources.values()),
                confidence=CONF_HIGH,
            )
        )
    return result, parsed
