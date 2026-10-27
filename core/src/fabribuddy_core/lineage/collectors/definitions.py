"""
Definitions collector: getDefinition -> code_artifact -> parsers -> edges.

Contributor tier, opt-in per workspace. Two passes on purpose:

  fetch  writes raw text + hash into `code_artifact`
  parse  runs the parsers over stored artifacts

Splitting them means a parser improvement re-runs over what we already hold
instead of re-fetching every notebook in the tenant — getDefinition is an LRO
with real rate limits, so re-fetching is the expensive part.
"""

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...fabric import definitions as definitions_api
from ...fabric.client import AccessDenied, FabricClient, FabricError
from ...models import utcnow
from ...services import progress
from .. import ids, layers, repository, resolve
from ..models import KIND_MODEL_TABLE, KIND_REPORT_VISUAL, CodeArtifact
from ..models import ParseResult as ParseResultRow
from ..parsers import eventstream as eventstream_parser
from ..parsers import mashup as mashup_parser
from ..parsers import mirroring as mirroring_parser
from ..parsers import notebook as notebook_parser
from ..parsers import pbir as pbir_parser
from ..parsers import pipeline as pipeline_parser
from ..parsers import tmdl as tmdl_parser

log = logging.getLogger("lineage.collect.definitions")

# Bumped when a parser changes behaviour, so stored results are recognised as
# stale and re-parsed without re-fetching.
PARSER_VERSION = 15

# Concurrent getDefinition operations. Modest on purpose: the Fabric API
# throttles per principal, and a 429 storm is slower than a queue.
FETCH_WORKERS = 6

PARSEABLE_TYPES = (
    "Notebook",
    "DataPipeline",
    "Dataflow",
    "SemanticModel",
    "Report",
    # Not lineage themselves: a Variable Library supplies the values notebooks
    # and dataflows resolve at run time, and a mirrored database declares its
    # source system.
    "VariableLibrary",
    "MirroredDatabase",
    # Streams events into lakehouse tables (its Lakehouse destinations).
    "Eventstream",
)

# Synthetic part holding a Variable Library's active value set, which is an item
# property rather than part of the definition.
ACTIVE_VALUE_SET_PART = ".activeValueSet"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch(
    session: Session,
    client: FabricClient,
    *,
    customer_id: str,
    tenant_id: str,
    contributor_workspaces: set[str],
    incremental: bool = False,
    changed_workspaces: set[str] | None = None,
) -> dict:
    """
    Fetch definitions for parseable items in scope.

    `incremental` skips an item whose stored code is at least as new as its
    last modification. Items with no modified time are skipped when
    `changed_workspaces` is known and does not include their workspace.
    """
    contributor_workspaces = {w.lower() for w in contributor_workspaces}
    errors: list[dict] = []
    counts = {
        "items": 0,
        "artifacts": 0,
        "skipped": 0,
        "unchanged": 0,
        "notModified": 0,
    }
    fetched_at: dict[str, object] = {}
    if incremental:
        from sqlalchemy import func

        fetched_at = dict(
            session.execute(
                select(CodeArtifact.item_id, func.max(CodeArtifact.fetched_at))
                .where(
                    CodeArtifact.customer_id == customer_id,
                    CodeArtifact.tenant_id == tenant_id,
                )
                .group_by(CodeArtifact.item_id)
            ).all()
        )

    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = [
        i
        for i in repository.list_items(session, customer_id, tenant_id)
        if i.item_type in PARSEABLE_TYPES
    ]

    todo = []
    for item in items:
        workspace = workspaces.get(item.workspace_id)
        if workspace is None:
            continue
        if workspace.workspace_guid.lower() not in contributor_workspaces:
            # Outside the SPN's Contributor scope: not ours to document. Counted,
            # not reported per item — that would be one gap row per item in
            # every workspace the customer deliberately left out.
            counts["skipped"] += 1
            continue
        if incremental and _unchanged(item, workspace, fetched_at, changed_workspaces):
            counts["notModified"] += 1
            continue
        todo.append((item, workspace))

    def get(pair):
        item, workspace = pair
        parts = definitions_api.get_definition(
            client, workspace.workspace_guid, item.item_guid, item.item_type
        )
        if item.item_type == "VariableLibrary":
            parts[ACTIVE_VALUE_SET_PART] = definitions_api.get_active_value_set(
                client, workspace.workspace_guid, item.item_guid
            )
        return parts

    # getDefinition is an LRO whose Retry-After is ~20s, so one at a time a
    # few hundred items is well over an hour of mostly sleeping. The calls run
    # concurrently — each still honouring its own Retry-After, and 429s are
    # retried by the client — while every DB write stays on this thread,
    # because the session is not thread-safe.
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fl-def") as pool:
        futures = {pool.submit(get, pair): pair for pair in todo}
        for done, future in enumerate(as_completed(futures), start=1):
            item, workspace = futures[future]
            progress.tick(done, len(todo), f"{item.item_type} {item.display_name}")
            if done % 25 == 0 or done == len(todo):
                log.info("definitions %d/%d", done, len(todo))
            try:
                parts = future.result()
            except (AccessDenied, FabricError) as e:
                reason = definitions_api.classify_denial(e, item.sensitivity_label)
                errors.append(
                    {
                        "scope": "item",
                        "scope_id": item.id,
                        "scope_name": f"{workspace.name} / {item.display_name}",
                        "reason": reason,
                        "message": (
                            "Code unavailable — protected sensitivity label."
                            if reason == "protected_label"
                            else str(e)[:500]
                        ),
                    }
                )
                continue
            _store(session, item, parts, customer_id, tenant_id, counts)

    session.commit()
    return {"stats": counts, "errors": errors}


def _unchanged(item, workspace, fetched_at: dict, changed_workspaces) -> bool:
    """Whether an incremental pass may skip this item's getDefinition."""
    last = fetched_at.get(item.id)
    if last is None or item.item_type == "VariableLibrary":
        # Never fetched — or a Variable Library, whose active value set can
        # change without the item's modified time moving.
        return False
    if item.modified_at is not None:
        return item.modified_at <= last
    if changed_workspaces is not None:
        return workspace.workspace_guid.lower() not in changed_workspaces
    return False


def _store(
    session, item, parts: dict, customer_id, tenant_id, counts, fetch_mode: str = "api"
) -> None:
    counts["items"] += 1
    for path, text in parts.items():
        digest = _sha(text)
        existing = session.scalars(
            select(CodeArtifact).where(
                CodeArtifact.item_id == item.id, CodeArtifact.part_path == path
            )
        ).first()
        if existing is not None and existing.sha256 == digest:
            existing.fetched_at = utcnow()
            counts["unchanged"] += 1
            continue
        if existing is None:
            existing = CodeArtifact(
                customer_id=customer_id,
                tenant_id=tenant_id,
                item_id=item.id,
                part_path=path,
            )
            session.add(existing)
        existing.language = definitions_api.language_for(path)
        existing.text = text
        existing.sha256 = digest
        existing.fetched_at = utcnow()
        existing.fetch_mode = fetch_mode
        counts["artifacts"] += 1
    session.commit()


def parse(
    session: Session, *, customer_id: str, tenant_id: str, force: bool = False
) -> dict:
    """
    Run the parsers over stored artifacts and write the graph.

    No network. Every stored artifact is parsed and resolved on every pass
    (see the loop); `force` is kept for callers and changes nothing.
    """
    errors: list[dict] = []
    counts = {
        "itemsParsed": 0,
        "edges": 0,
        "columnEdges": 0,
        "externalNodes": 0,
        "unresolved": 0,
        "skipped": 0,
    }

    resolver = resolve.Resolver(session, customer_id, tenant_id)
    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = {i.id: i for i in repository.list_items(session, customer_id, tenant_id)}

    artifacts_by_item: dict[str, list[CodeArtifact]] = {}
    stmt = select(CodeArtifact).where(
        CodeArtifact.customer_id == customer_id, CodeArtifact.tenant_id == tenant_id
    )
    for artifact in session.scalars(stmt):
        artifacts_by_item.setdefault(artifact.item_id, []).append(artifact)

    context = _Context.build(items, workspaces, artifacts_by_item, resolver)
    invocation_index = _InvocationIndex(items.values())

    # (pipeline item, notebook reference, parameters it passes), applied once
    # every item has been parsed.
    parameter_runs: list[tuple] = []

    for item_id, artifacts in artifacts_by_item.items():
        item = items.get(item_id)
        if item is None:
            continue
        workspace = workspaces.get(item.workspace_id)
        workspace_guid = workspace.workspace_guid if workspace else ""

        parser_name = item.item_type
        # The context (Variable Library values, %run neighbours) is part of what
        # a parse depends on: a changed value set must re-parse unchanged code.
        combined_sha = _sha(
            "|".join(sorted(a.sha256 for a in artifacts)) + context.fingerprint(item)
        )
        # Never skipped for unchanged code. What an edge resolves to depends on
        # the rest of the estate as well: a dataflow parsed while its target
        # lakehouse was out of scope drew nothing, and skipping it once that
        # lakehouse is back would leave its tables with no lineage for good.
        # Parsing is local and takes seconds; stale lineage is the costly bug.

        try:
            result, extra = _run_parser(item, artifacts, context)
        except Exception as e:  # noqa: BLE001 — one bad item, not the run
            log.exception("parser crashed for %s", item.display_name)
            errors.append(
                {
                    "scope": "item",
                    "scope_id": item.id,
                    "scope_name": item.display_name,
                    "reason": "error",
                    "message": f"Parser failed: {type(e).__name__}: {str(e)[:300]}",
                }
            )
            continue

        if result is None:
            counts["skipped"] += 1
            continue

        # Each item's writes in a savepoint: a database error on one item (a
        # missing parent row, say) is that item's gap, never the whole parse.
        try:
            with session.begin_nested():
                _apply_item(
                    session,
                    resolver,
                    item,
                    workspace,
                    workspace_guid,
                    result,
                    extra,
                    counts,
                    invocation_index,
                    customer_id,
                    tenant_id,
                )
        except Exception as e:  # noqa: BLE001
            repository.forget_pending(session)
            log.exception("storing the parse of %s failed", item.display_name)
            errors.append(
                {
                    "scope": "item",
                    "scope_id": item.id,
                    "scope_name": item.display_name,
                    "reason": "error",
                    "message": f"Could not store its lineage: {type(e).__name__}: "
                    f"{str(e).splitlines()[0][:300]}",
                }
            )
            continue
        applied = counts.pop("_applied", {})
        parameter_runs.extend(
            (item, ref, passed) for ref, passed in result.invoke_parameters
        )

        _record_parse(
            session,
            customer_id,
            tenant_id,
            artifacts,
            parser_name,
            combined_sha,
            result,
            applied,
        )

        for warning in result.warnings:
            errors.append(
                {
                    "scope": "item",
                    "scope_id": item.id,
                    "scope_name": item.display_name,
                    "reason": "unsupported",
                    "message": warning,
                }
            )
        session.commit()

    counts["parameterRuns"] = _apply_parameter_runs(
        session,
        resolver,
        parameter_runs,
        invocation_index,
        artifacts_by_item,
        context,
        workspaces,
        errors,
    )
    session.commit()

    repository.prune_star_columns(session, customer_id, tenant_id)
    session.commit()
    repository.prune_island_externals(session, customer_id, tenant_id)

    for message in resolver.unresolved[:100]:
        errors.append(
            {
                "scope": "lineage",
                "scope_name": "unresolved reference",
                "reason": "unsupported",
                "message": message,
            }
        )

    session.commit()
    return {"stats": counts, "errors": errors}


# What Dataflow Gen2 stamps into a Delta commit's engineInfo.
MASHUP_ENGINE = "mashup engine"


def attribute_dormant_writes(session: Session, *, customer_id: str, tenant_id: str) -> dict:
    """
    Give writerless tables the dataflow that, by the evidence, wrote them.

    A dataflow's definition says what it loads *today*. Switch a query's loading
    off, or drop its destination, and the table it used to fill stays in the
    lakehouse with nothing in the current code naming it as a target — so it
    would show no writer at all. Two independent facts together identify the
    writer anyway:

    - the Delta log says the Mashup Engine (Dataflow Gen2) committed to it, and
    - exactly one dataflow has a query of that name whose tables land in that
      lakehouse (its default destination, or one of its explicit ones).

    The edge is `inferred` and low-confidence, and says why. It is drawn only
    for a table no other item writes, and only when exactly one dataflow
    matches; two candidates are reported, never guessed between. Every pass
    recomputes the set from scratch, because a dataflow that turns loading back
    on turns the guess into a parsed edge.
    """
    from sqlalchemy import delete, func

    from ..models import (
        SOURCE_INFERRED,
        STORAGE_KINDS,
        DataNode,
        DeltaVersion,
        Item,
        LineageEdge,
    )
    from ..parsers.base import ParseResult, Write

    dataflow_ids = select(Item.id).where(
        Item.customer_id == customer_id,
        Item.tenant_id == tenant_id,
        Item.item_type == "Dataflow",
    )
    session.execute(
        delete(LineageEdge).where(
            LineageEdge.customer_id == customer_id,
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.source == SOURCE_INFERRED,
            LineageEdge.via_item_id.in_(dataflow_ids),
        )
    )
    session.flush()

    written = select(LineageEdge.to_node_id).where(
        LineageEdge.customer_id == customer_id, LineageEdge.tenant_id == tenant_id
    )
    writerless = {
        node_id
        for node_id in session.scalars(
            select(DataNode.id).where(
                DataNode.customer_id == customer_id,
                DataNode.tenant_id == tenant_id,
                DataNode.kind.in_(STORAGE_KINDS),
                DataNode.is_deleted.is_(False),
                DataNode.id.not_in(written),
            )
        )
    }
    counts = {"tables": 0, "ambiguous": 0}
    if not writerless:
        return {"stats": counts, "errors": []}

    # The last commit each writerless table got from the Mashup Engine.
    by_mashup: dict[str, object] = dict(
        session.execute(
            select(DeltaVersion.node_id, func.max(DeltaVersion.committed_at))
            .where(
                DeltaVersion.customer_id == customer_id,
                DeltaVersion.tenant_id == tenant_id,
                DeltaVersion.node_id.in_(writerless),
                func.lower(DeltaVersion.engine_info).like(f"%{MASHUP_ENGINE}%"),
            )
            .group_by(DeltaVersion.node_id)
        ).all()
    )
    if not by_mashup:
        return {"stats": counts, "errors": []}

    resolver = resolve.Resolver(session, customer_id, tenant_id)
    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = {i.id: i for i in repository.list_items(session, customer_id, tenant_id)}
    dataflows = [i for i in items.values() if i.item_type == "Dataflow"]
    # Every item's artifacts, not only the dataflows': the context resolves
    # Variable Library values, and a dataflow reading its lakehouse through
    # `Variable.ValueOrDefault` would otherwise point at the design-time default.
    artifacts_by_item: dict[str, list[CodeArtifact]] = {}
    for artifact in session.scalars(
        select(CodeArtifact).where(
            CodeArtifact.customer_id == customer_id,
            CodeArtifact.tenant_id == tenant_id,
        )
    ):
        artifacts_by_item.setdefault(artifact.item_id, []).append(artifact)
    context = _Context.build(items, workspaces, artifacts_by_item, resolver)

    # table node -> [(dataflow, dormant write)]
    claims: dict[str, list[tuple]] = {}
    for dataflow in dataflows:
        artifacts = artifacts_by_item.get(dataflow.id)
        if not artifacts:
            continue
        try:
            result, _extra = _run_parser(dataflow, artifacts, context)
        except Exception:  # noqa: BLE001 — parse() already recorded this failure
            log.debug("attribution: parser failed for %s", dataflow.display_name)
            continue
        if result is None:
            continue
        workspace = workspaces.get(dataflow.workspace_id)
        before = len(resolver.unresolved)
        for write in result.dormant_writes:
            node_id = resolver.resolve_table(
                write.table,
                workspace_guid=workspace.workspace_guid if workspace else "",
                create_external=False,
            )
            if node_id in by_mashup:
                claims.setdefault(node_id, []).append((dataflow, write))
        # A dormant name that matches no table is the normal case, not a gap.
        del resolver.unresolved[before:]

    errors: list[dict] = []
    for node_id, candidates in claims.items():
        by_item = {dataflow.id: (dataflow, write) for dataflow, write in candidates}
        if len(by_item) > 1:
            counts["ambiguous"] += 1
            errors.append(
                {
                    "scope": "lineage",
                    "scope_name": "dataflow attribution",
                    "reason": "unsupported",
                    "message": (
                        f"{node_id.rsplit(':', 1)[-1]}: written by Dataflow Gen2, and "
                        f"{len(by_item)} dataflows have a query of that name ("
                        + ", ".join(sorted(d.display_name for d, _ in by_item.values()))
                        + ") — not attributed."
                    ),
                }
            )
            continue
        dataflow, write = next(iter(by_item.values()))
        last = by_mashup[node_id]
        when = f"{last:%Y-%m-%d}" if last else "an earlier run"
        inferred = Write(
            table=write.table,
            mode=write.mode,
            via_ref=f"{write.via_ref} (loading off; last Dataflow write {when})"[:200],
            reads=write.reads,
            confidence=write.confidence,
        )
        workspace = workspaces.get(dataflow.workspace_id)
        resolve.apply_parse_result(
            session,
            resolver,
            ParseResult(writes=[inferred]),
            via_item_id=dataflow.id,
            workspace_guid=workspace.workspace_guid if workspace else "",
            source=SOURCE_INFERRED,
        )
        counts["tables"] += 1

    session.commit()
    return {"stats": counts, "errors": errors}


def _record_parse(
    session, customer_id, tenant_id, artifacts, parser_name, combined_sha, result, applied
):
    """
    Store what the parser made of each artifact.

    Kept separate from the edges so "we parsed this and found nothing" is
    distinguishable from "we never parsed this" — which is exactly what the
    unresolved-lineage finding needs to point at.
    """
    for artifact in artifacts:
        row = session.scalars(
            select(ParseResultRow).where(
                ParseResultRow.artifact_id == artifact.id,
                ParseResultRow.parser == parser_name,
            )
        ).first()
        if row is None:
            row = ParseResultRow(
                customer_id=customer_id,
                tenant_id=tenant_id,
                artifact_id=artifact.id,
                parser=parser_name,
            )
            session.add(row)
        row.parser_version = PARSER_VERSION
        row.artifact_sha256 = combined_sha
        row.warnings = result.warnings[:50]
        row.confidence = result.confidence
        row.edges_found = applied.get("edges", 0)
        row.column_edges_found = applied.get("columnEdges", 0)
        row.parsed_at = utcnow()


class _Context:
    """
    What a parse needs to know beyond the item's own code, per workspace.

    - Variable Library values in effect (defaults + the active value set), so
      `Variable.ValueOrDefault(...)` and `notebookutils.variableLibrary` resolve
      to what actually runs in that workspace.
    - The workspace's notebooks by name, for `%run`.
    - The tables a mirrored database holds, for whole-database mirroring.
    """

    def __init__(self):
        self.variables: dict[str, dict[str, str]] = {}
        self.notebooks: dict[str, dict[str, str]] = {}
        self.item_tables: dict[str, list[tuple[str, str]]] = {}
        self.workspace_guid: dict[str, str] = {}
        self.workspace_name: dict[str, str] = {}
        # (workspace db id, model name lower) -> model GUID, for `byPath` reports.
        self.models_by_name: dict[tuple[str, str], str] = {}

    @classmethod
    def build(cls, items, workspaces, artifacts_by_item, resolver) -> "_Context":
        ctx = cls()
        ctx.workspace_guid = {w.id: w.workspace_guid for w in workspaces.values()}
        ctx.workspace_name = {w.id: w.name for w in workspaces.values()}
        ctx.models_by_name = {
            (i.workspace_id, i.display_name.lower()): i.item_guid
            for i in items.values()
            if i.item_type == "SemanticModel"
        }
        for item_id, artifacts in artifacts_by_item.items():
            item = items.get(item_id)
            if item is None:
                continue
            parts = {a.part_path: a.text for a in artifacts}
            if item.item_type == "VariableLibrary":
                active = parts.pop(ACTIVE_VALUE_SET_PART, "")
                ctx.variables.setdefault(item.workspace_id, {}).update(
                    mirroring_parser.variable_values(parts, item.display_name, active)
                )
            elif item.item_type == "Notebook":
                text = next(
                    (t for p, t in parts.items() if p.endswith("notebook-content.py")), ""
                )
                if text:
                    ctx.notebooks.setdefault(item.workspace_id, {})[item.display_name] = (
                        text
                    )
        for node in resolver.nodes:
            if node.item_id and node.table_name:
                ctx.item_tables.setdefault(node.item_id, []).append(
                    (node.schema_name or "", node.table_name)
                )
        return ctx

    def fingerprint(self, item) -> str:
        if item.item_type not in ("Notebook", "Dataflow", "MirroredDatabase"):
            return ""
        variables = self.variables.get(item.workspace_id, {})
        notebooks = (
            self.notebooks.get(item.workspace_id, {})
            if item.item_type == "Notebook"
            else {}
        )
        tables = (
            self.item_tables.get(item.id, [])
            if item.item_type == "MirroredDatabase"
            else []
        )
        return _sha(
            json.dumps(
                [
                    sorted(variables.items()),
                    sorted((k, _sha(v)) for k, v in notebooks.items()),
                    tables,
                ],
                sort_keys=True,
            )
        )


def _run_parser(item, artifacts: list[CodeArtifact], context: "_Context | None" = None):
    """Dispatch to the right parser. Returns (ParseResult | None, extra info)."""
    context = context or _Context()
    by_path = {a.part_path: a.text for a in artifacts}
    variables = context.variables.get(item.workspace_id, {})

    if item.item_type == "Notebook":
        text = next(
            (t for p, t in by_path.items() if p.endswith("notebook-content.py")), ""
        )
        if not text:
            return None, None
        return (
            notebook_parser.parse_notebook(
                text,
                run_sources=context.notebooks.get(item.workspace_id, {}),
                variables=variables,
                workspace_name=context.workspace_name.get(item.workspace_id, ""),
                workspace_id=context.workspace_guid.get(item.workspace_id, ""),
            ),
            None,
        )

    if item.item_type in ("Warehouse", "SQLEndpoint", "SQLAnalyticsEndpoint"):
        return (
            _parse_warehouse_sql(
                by_path,
                workspace_guid=context.workspace_guid.get(item.workspace_id, ""),
                database=item.display_name,
            ),
            None,
        )

    if item.item_type == "MirroredDatabase":
        text = next(
            (t for p, t in by_path.items() if p.lower().endswith("mirroring.json")), ""
        )
        if not text:
            return None, None
        return (
            mirroring_parser.parse_mirroring(
                text,
                workspace_guid=context.workspace_guid.get(item.workspace_id, ""),
                item_guid=item.item_guid,
                tables=context.item_tables.get(item.id, []),
            ),
            None,
        )

    if item.item_type == "Eventstream":
        text = next(
            (t for p, t in by_path.items() if p.lower().endswith("eventstream.json")), ""
        )
        if not text:
            return None, None
        return (
            eventstream_parser.parse_eventstream(
                text, workspace_guid=context.workspace_guid.get(item.workspace_id, "")
            ),
            None,
        )

    if item.item_type == "DataPipeline":
        text = next(
            (t for p, t in by_path.items() if p.endswith("pipeline-content.json")), ""
        )
        if not text:
            return None, None
        return pipeline_parser.parse_pipeline(json.loads(text), variables=variables), None

    if item.item_type == "Dataflow":
        mashup = next((t for p, t in by_path.items() if p.endswith(".pq")), "")
        metadata_text = next(
            (t for p, t in by_path.items() if "querymetadata" in p.lower()), "{}"
        )
        if not mashup:
            return None, None
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError:
            metadata = {}
        return mashup_parser.parse_mashup(mashup, metadata, variables=variables), None

    if item.item_type == "SemanticModel":
        documents = {p: t for p, t in by_path.items() if p.lower().endswith(".tmdl")}
        if not documents:
            return None, None
        result, info = tmdl_parser.parse_tmdl(documents, model_item_id=item.item_guid)
        return result, info

    if item.item_type == "Report":
        visuals = []
        pages: dict[str, str] = {}
        for path, text in by_path.items():
            lowered = path.lower()
            if lowered.endswith("page.json"):
                try:
                    pages[_page_of(path)] = (json.loads(text) or {}).get(
                        "displayName"
                    ) or ""
                except json.JSONDecodeError:
                    pass
            if not lowered.endswith("visual.json"):
                continue
            try:
                content = json.loads(text)
            except json.JSONDecodeError:
                continue
            visuals.append((_page_of(path), content))
        if not visuals:
            return None, None
        definition = next(
            (t for p, t in by_path.items() if p.lower().endswith("definition.pbir")), ""
        )
        model_guid, model_name = pbir_parser.model_reference(definition)
        if not model_guid and model_name:
            model_guid = context.models_by_name.get(
                (item.workspace_id, model_name.lower()), ""
            )
        result, parsed = pbir_parser.parse_report(
            visuals,
            model_name=model_name,
            model_item_id=model_guid,
            report_item_id=item.item_guid,
        )
        if not model_guid:
            result.warn(
                "Report's semantic model could not be identified from definition.pbir — "
                "fields were matched by table name only."
            )
        return result, {"visuals": parsed, "pages": pages}

    return None, None


_COPY_INTO = re.compile(
    r"COPY\s+INTO\s+(?P<target>[\w\[\]\.\"]+).*?FROM\s+'(?P<url>[^']+)'",
    re.IGNORECASE | re.DOTALL,
)


def _parse_warehouse_sql(parts: dict[str, str], *, workspace_guid: str, database: str):
    """
    T-SQL modules and query-insights statements of one warehouse / SQL endpoint.

    Names are placed the way T-SQL resolves them: `schema.table` in this
    database, `db.schema.table` in another item of the same workspace.
    """
    from ..parsers.base import ParseResult, TableRef, Write
    from ..parsers.sql import parse_sql

    def place(ref: TableRef) -> TableRef:
        if ref.is_external or ref.path or ref.dynamic_expr or not ref.table:
            return ref
        return TableRef(
            workspace_id=workspace_guid,
            lakehouse=ref.lakehouse or database,
            schema=ref.schema or "dbo",
            table=ref.table,
        )

    result = ParseResult()
    for path, text in sorted(parts.items()):
        if not path.endswith(".sql") or not text:
            continue
        copy = _COPY_INTO.search(text)
        if copy:
            from ..parsers.base import parse_qualified

            target = parse_qualified(copy.group("target").replace("[", "").replace("]", ""))
            result.writes.append(
                Write(
                    table=place(target),
                    mode="copy_into",
                    via_ref=path,
                    reads=[
                        TableRef(
                            table=copy.group("url").rsplit("/", 1)[-1] or copy.group("url"),
                            is_external=True,
                            connection={
                                "kind": "storage",
                                "url": copy.group("url").split("?")[0],
                            },
                        )
                    ],
                )
            )
            continue
        parsed = parse_sql(text, via_ref=path, dialect="tsql")
        for write in parsed.writes:
            write.table = place(write.table)
            write.reads[:] = [place(r) for r in write.reads]
            write.column_edges[:] = []  # table-level for warehouse SQL
        parsed.column_edges[:] = []
        parsed.reads[:] = [place(r) for r in parsed.reads]
        # Statements that only read (a SELECT in history) are not lineage.
        result.writes.extend(parsed.writes)
        for warning in parsed.warnings[:3]:
            result.warn(f"{path}: {warning}")
    return result


def _page_of(path: str) -> str:
    """`definition/pages/Overview/visuals/v1/visual.json` -> `Overview`."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if "pages" in parts:
        index = parts.index("pages")
        if index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _apply_parameter_runs(
    session,
    resolver,
    runs,
    invocation_index,
    artifacts_by_item,
    context,
    workspaces,
    errors,
) -> int:
    """
    A notebook run by a pipeline with parameters, parsed with those values.

    The notebook's own parse uses its parameter cell's defaults; each distinct
    set of values a pipeline passes is parsed once more and its writes added
    (same notebook, so its Code tab and page show them). Returns how many runs
    were applied.
    """
    seen: set[tuple[str, tuple]] = set()
    applied = 0
    for pipeline, reference, passed in runs:
        notebook = invocation_index.find(pipeline, reference)
        if notebook is None or notebook.item_type != "Notebook":
            continue
        key = (notebook.id, tuple(sorted(passed.items())))
        if key in seen:
            continue
        seen.add(key)
        text = next(
            (
                a.text
                for a in artifacts_by_item.get(notebook.id, [])
                if a.part_path.endswith("notebook-content.py")
            ),
            "",
        )
        if not text:
            continue
        workspace = workspaces.get(notebook.workspace_id)
        try:
            result = notebook_parser.parse_notebook(
                text,
                run_sources=context.notebooks.get(notebook.workspace_id, {}),
                variables=context.variables.get(notebook.workspace_id, {}),
                parameters=passed,
                workspace_name=context.workspace_name.get(notebook.workspace_id, ""),
                workspace_id=context.workspace_guid.get(notebook.workspace_id, ""),
            )
            with session.begin_nested():
                resolve.apply_parse_result(
                    session,
                    resolver,
                    result,
                    via_item_id=notebook.id,
                    workspace_guid=workspace.workspace_guid if workspace else "",
                )
            applied += 1
        except Exception as e:  # noqa: BLE001 — one run, not the parse
            repository.forget_pending(session)
            log.exception("parameterised parse of %s failed", notebook.display_name)
            errors.append(
                {
                    "scope": "item",
                    "scope_id": notebook.id,
                    "scope_name": notebook.display_name,
                    "reason": "error",
                    "message": f"Run from {pipeline.display_name} with "
                    f"{', '.join(f'{k}={v}' for k, v in passed.items())} could not be "
                    f"parsed: {type(e).__name__}",
                }
            )
    return applied


def _apply_item(
    session,
    resolver,
    item,
    workspace,
    workspace_guid,
    result,
    extra,
    counts,
    invocation_index,
    customer_id,
    tenant_id,
) -> None:
    """Store one item's parse: its node (report/model), edges, invocations, model rows."""
    if item.item_type == "Report" and extra:
        _report_node(session, resolver, item, workspace, extra)
        extra = None
    if item.item_type == "SemanticModel":
        _ensure_model_node(session, resolver, item, workspace)
    repository.clear_parsed_edges(session, customer_id, tenant_id, item.id)
    applied = resolve.apply_parse_result(
        session,
        resolver,
        result,
        via_item_id=item.id,
        workspace_guid=workspace_guid,
    )
    for key in ("edges", "columnEdges", "externalNodes", "unresolved"):
        counts[key] += applied.get(key, 0)
    counts["invocations"] = counts.get("invocations", 0) + _store_invocations(
        session, customer_id, tenant_id, item, result.invokes, invocation_index
    )
    counts["itemsParsed"] += 1

    if extra:
        _apply_model_info(item, extra)
        if item.item_type == "SemanticModel":
            _model_table_sources(session, resolver, item, workspace, extra)
    counts["_applied"] = applied


def _ensure_model_node(session, resolver, item, workspace) -> None:
    """
    The semantic model's node, when the Scanner (admin API) has not made it.

    Without the admin API the model is known only from the Fabric item list and
    its TMDL; its table rows and lineage still need the node to hang on.
    """
    ws_guid = workspace.workspace_guid if workspace else ""
    node_id = ids.semantic_model_id(ws_guid, item.item_guid)
    if repository.get_node(session, resolver.customer_id, resolver.tenant_id, node_id):
        return
    layer, declared = layers.resolve(KIND_MODEL_TABLE)
    node = repository.upsert_node(
        session,
        resolver.customer_id,
        resolver.tenant_id,
        id=node_id,
        kind=KIND_MODEL_TABLE,
        layer=layer,
        layer_is_declared=declared,
        workspace_id=item.workspace_id,
        item_id=item.id,
        schema_name="",
        table_name=item.display_name,
        display_path=" / ".join(
            p for p in ((workspace.name if workspace else ""), item.display_name) if p
        ),
    )
    resolver.register(node)
    session.flush()


def _report_node(session, resolver, item, workspace, info: dict) -> None:
    """
    One node for the whole report in the Report lane.

    Any per-visual nodes an earlier build created for this report are removed
    (with their edges, by cascade): the report is the unit people ask about.
    """
    from sqlalchemy import delete

    from ..models import DataNode

    ws_guid = workspace.workspace_guid if workspace else ""
    node_id = ids.report_id(ws_guid, item.item_guid)
    session.execute(
        delete(DataNode).where(
            DataNode.customer_id == resolver.customer_id,
            DataNode.tenant_id == resolver.tenant_id,
            DataNode.item_id == item.id,
            DataNode.kind == KIND_REPORT_VISUAL,
            DataNode.id != node_id,
        )
    )
    visuals = info.get("visuals") or []
    pages = sorted({v.page for v in visuals if v.page})
    node = repository.upsert_node(
        session,
        resolver.customer_id,
        resolver.tenant_id,
        id=node_id,
        kind=KIND_REPORT_VISUAL,
        layer=layers.layer_for_kind(KIND_REPORT_VISUAL),
        layer_is_declared=True,
        workspace_id=item.workspace_id,
        item_id=item.id,
        schema_name="",
        table_name=item.display_name,
        display_path=" / ".join(
            p for p in (workspace.name if workspace else "", item.display_name) if p
        ),
        connection_detail={"visuals": len(visuals), "pages": len(pages)},
    )
    resolver.register(node)
    session.flush()  # edges below carry FKs to this node


def _model_table_sources(session, resolver, item, workspace, info: dict) -> None:
    """
    Fill the model's table rows with where each table comes from.

    The model page's Tables tab then reads `Sales ← lh_gold.dbo.fact_sales`
    without a node per model table in the graph.
    """
    ws_guid = workspace.workspace_guid if workspace else ""
    model_node = ids.semantic_model_id(ws_guid, item.item_guid)
    labels = {n.id: n.display_path or n.table_name for n in resolver.nodes}
    for ordinal, table in enumerate(info.get("tables") or []):
        source = table.get("source")
        label = ""
        if source is not None:
            node_id = resolver.resolve_table(
                source, workspace_guid=ws_guid, create_external=True
            )
            label = labels.get(node_id or "", "") if node_id else ""
            if not label:
                label = (
                    ".".join(
                        p for p in (source.lakehouse, source.schema, source.table) if p
                    )
                    or (source.connection or {}).get("url", "")
                    or source.table
                )
        mode = (table.get("mode") or "").replace("directlake", "Direct Lake")
        measures_only = bool(table.get("measures")) and not table.get("columns")
        repository.upsert_column(
            session,
            resolver.customer_id,
            resolver.tenant_id,
            id=ids.column_id(model_node, table["name"]),
            node_id=model_node,
            name=table["name"],
            ordinal=ordinal,
            data_type="measures" if measures_only else (mode or "table"),
            description=f"← {label}" if label else "",
            is_measure=measures_only,
        )


def _apply_model_info(item, info: dict) -> None:
    """Carry TMDL facts onto the item row for the access and findings engines."""
    if info.get("storage_mode"):
        item.storage_mode = info["storage_mode"]
    if info.get("uses_fixed_identity"):
        item.uses_fixed_identity = True


class _InvocationIndex:
    """How an invocation's reference (a GUID or a name) finds its item."""

    def __init__(self, items):
        self.by_guid = {i.item_guid.lower(): i for i in items}
        self.by_name: dict[str, list] = {}
        for i in items:
            self.by_name.setdefault(i.display_name.lower(), []).append(i)

    def find(self, caller, reference: str):
        ref = (reference or "").strip().strip("\"'")
        found = self.by_guid.get(ref.lower())
        if found is not None:
            return found
        # `%run /Helpers/nb_config` or `notebook.run("nb_load")`: the last
        # path segment is the item's display name.
        name = ref.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
        candidates = self.by_name.get(name, [])
        same_workspace = [c for c in candidates if c.workspace_id == caller.workspace_id]
        if len(same_workspace) == 1:
            return same_workspace[0]
        # Across workspaces only when the name is unique in the whole tenant;
        # anything else is a guess, and a wrong orchestrator misleads.
        return candidates[0] if len(candidates) == 1 else None


def _store_invocations(session, customer_id, tenant_id, item, invokes, index) -> int:
    """Replace this item's parsed invocations with what the code says now."""
    from sqlalchemy import delete

    from ..models import ItemInvocation

    session.execute(delete(ItemInvocation).where(ItemInvocation.from_item_id == item.id))
    targets = {
        target.id
        for target in (index.find(item, ref) for ref in invokes or [])
        if target is not None and target.id != item.id
    }
    for target_id in targets:
        session.add(
            ItemInvocation(
                customer_id=customer_id,
                tenant_id=tenant_id,
                from_item_id=item.id,
                to_item_id=target_id,
            )
        )
    session.flush()
    return len(targets)
