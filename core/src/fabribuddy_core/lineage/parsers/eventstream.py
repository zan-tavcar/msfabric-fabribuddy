"""
Eventstream definitions (`eventstream.json`).

An eventstream is a graph: sources feed streams, streams feed operators, and
destinations take one or more of those as `inputNodes`. Shape per Microsoft
Learn, "Eventstream item definition":

    {"sources": [{"name": "orders-hub", "type": "AzureEventHub", "properties": {…}}],
     "streams": [{"name": "es-stream", "inputNodes": [{"name": "orders-hub"}], …}],
     "operators": [{"name": "filter", "type": "Filter", "inputNodes": […]}],
     "destinations": [{"name": "to-bronze", "type": "Lakehouse",
                       "properties": {"workspaceId": "…", "itemId": "…",
                                      "schema": "dbo", "deltaTable": "events"},
                       "inputNodes": [{"name": "filter"}]}]}

A Lakehouse destination is a table the eventstream appends to, from every
source upstream of it in the graph. Sources are outside the estate, so each is
an external node named after the source. Other destinations (Eventhouse,
Activator, custom endpoints) are not lakehouse or warehouse tables; they are
named in a warning rather than dropped.
"""

import json

from .base import CONF_HIGH, ParseResult, TableRef, Write

_EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


def parse_eventstream(text: str, *, workspace_guid: str = "") -> ParseResult:
    result = ParseResult()
    try:
        definition = json.loads(text or "{}")
    except json.JSONDecodeError:
        result.warn("eventstream.json is not valid JSON.")
        return result

    nodes: dict[str, dict] = {}
    for group in ("sources", "streams", "operators", "destinations"):
        for node in definition.get(group) or []:
            if isinstance(node, dict) and node.get("name"):
                nodes[node["name"]] = {**node, "_group": group}

    def upstream_sources(name: str, seen: set[str]) -> list[dict]:
        if name in seen or name not in nodes:
            return []
        seen.add(name)
        node = nodes[name]
        if node["_group"] == "sources":
            return [node]
        found: list[dict] = []
        for ref in node.get("inputNodes") or []:
            found.extend(upstream_sources((ref or {}).get("name", ""), seen))
        return found

    for destination in definition.get("destinations") or []:
        if not isinstance(destination, dict):
            continue
        kind = destination.get("type") or ""
        name = destination.get("name") or "destination"
        if kind.lower() != "lakehouse":
            result.warn(
                f"Eventstream destination '{name}' ({kind or 'unknown type'}) is not "
                "a lakehouse table; it is not shown in lineage."
            )
            continue
        properties = destination.get("properties") or {}
        table = properties.get("deltaTable") or ""
        if not table:
            result.warn(f"Eventstream destination '{name}' names no table.")
            continue
        workspace = properties.get("workspaceId") or ""
        if not workspace or workspace == _EMPTY_GUID:
            workspace = workspace_guid

        seen: set[str] = set()
        reads: list[TableRef] = []
        for ref in destination.get("inputNodes") or []:
            for source in upstream_sources((ref or {}).get("name", ""), seen):
                reads.append(
                    TableRef(
                        table=source["name"],
                        is_external=True,
                        connection={"kind": source.get("type") or "eventstream source"},
                    )
                )
        if not reads:
            result.warn(f"Eventstream destination '{name}': no source found upstream.")

        result.writes.append(
            Write(
                table=TableRef(
                    workspace_id=workspace,
                    item_id=properties.get("itemId") or "",
                    schema=properties.get("schema") or "",
                    table=table,
                ),
                mode="stream",
                via_ref=name,
                reads=reads,
                confidence=CONF_HIGH,
            )
        )
        result.reads.extend(reads)
    return result
