"""
The lineage graph.

Node and column primary keys are *deterministic natural keys* (see ids.py), so
a re-scan upserts rather than duplicating and a node id is stable enough to put
in a URL and a bookmark. Edges, versions, grants and findings get integer
surrogates instead: they are high-volume and nothing links to them by name.

Every row carries customer_id AND tenant_id. Isolation is enforced in the
repository layer, which makes both mandatory arguments.
"""

import datetime as dt

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ..models import utcnow

# --- vocabularies ----------------------------------------------------------

# What a data_node *is*.
KIND_LAKEHOUSE_TABLE = "lakehouse_table"
KIND_WAREHOUSE_TABLE = "warehouse_table"
KIND_MIRRORED_TABLE = "mirrored_table"
KIND_EXTERNAL = "external"
KIND_MODEL_TABLE = "model_table"
KIND_REPORT_VISUAL = "report_visual"
# Tables with physical storage in OneLake: a Delta log, a row count, a cadence.
STORAGE_KINDS = (KIND_LAKEHOUSE_TABLE, KIND_WAREHOUSE_TABLE, KIND_MIRRORED_TABLE)
# Items that hold tables rather than code (their page lists the tables).
STORAGE_ITEM_TYPES = {
    "Lakehouse": "Lakehouse",
    "Warehouse": "Warehouse",
    "MirroredDatabase": "Mirrored database",
    "SQLDatabase": "SQL database",
}

# Where it sits left-to-right in the graph: the storage it physically lives in.
# Decided by the kind alone — never by a naming convention, which a tenant may
# not follow and which would put a table in a lane it does not belong to.
LAYER_SOURCE = "source"
LAYER_MIRRORED = "mirrored"
LAYER_LAKEHOUSE = "lakehouse"
LAYER_WAREHOUSE = "warehouse"
LAYER_SEMANTIC = "semantic"
LAYER_REPORT = "report"
LAYERS = (
    LAYER_SOURCE,
    LAYER_MIRRORED,
    LAYER_LAKEHOUSE,
    LAYER_WAREHOUSE,
    LAYER_SEMANTIC,
    LAYER_REPORT,
)
LAYER_BY_KIND = {
    KIND_EXTERNAL: LAYER_SOURCE,
    KIND_MIRRORED_TABLE: LAYER_MIRRORED,
    KIND_LAKEHOUSE_TABLE: LAYER_LAKEHOUSE,
    KIND_WAREHOUSE_TABLE: LAYER_WAREHOUSE,
    KIND_MODEL_TABLE: LAYER_SEMANTIC,
    KIND_REPORT_VISUAL: LAYER_REPORT,
}

# How we learned about an edge. Ordered weakest to strongest: a runtime
# observation always beats a static parse, which always beats a guess.
SOURCE_INFERRED = "inferred"
SOURCE_PARSED = "parsed"
SOURCE_DECLARED = "declared"
SOURCE_RUNTIME = "runtime"
EDGE_SOURCE_RANK = {
    SOURCE_INFERRED: 0,
    SOURCE_PARSED: 1,
    SOURCE_DECLARED: 2,
    SOURCE_RUNTIME: 3,
}

CONF_LOW = "low"
CONF_MEDIUM = "medium"
CONF_HIGH = "high"

# Where a description came from. A human edit outranks anything collected.
DOC_DELTA_PROPERTY = "delta_property"
DOC_TMDL = "tmdl"
DOC_USER = "user"
DOC_SOURCE_RANK = {DOC_DELTA_PROPERTY: 0, DOC_TMDL: 1, DOC_USER: 2}


class Workspace(Base):
    """A Fabric workspace. id is `ws:{workspaceGuid}`."""

    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    workspace_guid: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    ws_type: Mapped[str] = mapped_column(String(40), default="")
    state: Mapped[str] = mapped_column(String(40), default="")
    capacity_id: Mapped[str] = mapped_column(String(36), default="")
    capacity_sku: Mapped[str] = mapped_column(String(20), default="")

    # Whether the SPN holds Contributor here — decides whether code and Delta
    # history are collectable, and drives the "code unavailable" notes.
    has_contributor: Mapped[bool] = mapped_column(Boolean, default=False)
    git_provider: Mapped[str] = mapped_column(String(40), default="")
    git_status: Mapped[str] = mapped_column(String(40), default="")

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_workspace_tenant", "tenant_id", "customer_id"),)


class Item(Base):
    """
    A Fabric item: Lakehouse, Notebook, DataPipeline, Dataflow, SemanticModel,
    Report, SQLEndpoint, Warehouse. id is `item:{wsGuid}:{itemGuid}`.
    """

    __tablename__ = "item"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )

    item_guid: Mapped[str] = mapped_column(String(36), nullable=False)
    item_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    sensitivity_label: Mapped[str] = mapped_column(String(120), default="")
    endorsement: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[str] = mapped_column(String(320), default="")
    modified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # Semantic models only: Import / DirectQuery / DirectLake / Composite, and
    # whether the model connects with a fixed identity (the indirect-access
    # finding depends on this).
    storage_mode: Mapped[str] = mapped_column(String(40), default="")
    uses_fixed_identity: Mapped[bool] = mapped_column(Boolean, default=False)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped[Workspace] = relationship()

    __table_args__ = (Index("ix_item_tenant_type", "tenant_id", "item_type"),)


class DataNode(Base):
    """
    A table-level node in the lineage graph: a lakehouse table, an external
    source, a semantic-model table, or a report visual.
    """

    __tablename__ = "data_node"

    id: Mapped[str] = mapped_column(String(400), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    layer: Mapped[str] = mapped_column(String(20), default=LAYER_SOURCE, index=True)
    # True when the layer came from a name rule, False when it fell back to
    # topological depth — worth showing, because a fallback is a guess.
    layer_is_declared: Mapped[bool] = mapped_column(Boolean, default=False)

    workspace_id: Mapped[str | None] = mapped_column(
        String(80), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=True
    )
    # The item that CONTAINS the node (lakehouse / model / report). The item
    # that WRITES it is on the edge, not here.
    item_id: Mapped[str | None] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=True
    )

    schema_name: Mapped[str] = mapped_column(String(200), default="")
    table_name: Mapped[str] = mapped_column(String(400), default="")
    # Human path for the header and search: "Sales Data [Prod] / lh_silver / sales.customers"
    display_path: Mapped[str] = mapped_column(String(1000), default="")

    # External nodes only: a hash of the connection details plus the object, so
    # the same SQL server reached from two pipelines is ONE node.
    connection_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    connection_detail: Mapped[dict] = mapped_column(JSON, default=dict)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # Soft delete: a table that vanished still has history worth keeping, and
    # hard-deleting it would cascade away its Delta versions.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    workspace: Mapped[Workspace | None] = relationship()
    item: Mapped[Item | None] = relationship()
    columns: Mapped[list["ColumnNode"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_data_node_tenant_layer", "tenant_id", "layer"),
        Index("ix_data_node_item", "item_id"),
    )


class ColumnNode(Base):
    """One column. id is `{nodeId}#{columnName}`."""

    __tablename__ = "column_node"

    id: Mapped[str] = mapped_column(String(700), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(400), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    data_type: Mapped[str] = mapped_column(String(100), default="")
    nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    is_pii: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Semantic-model columns: whether this is a measure rather than a column.
    is_measure: Mapped[bool] = mapped_column(Boolean, default=False)

    node: Mapped[DataNode] = relationship(back_populates="columns")

    __table_args__ = (
        UniqueConstraint("node_id", "name", name="uq_column_per_node"),
        Index("ix_column_tenant_name", "tenant_id", "name"),
    )


class LineageEdge(Base):
    """
    A table-level edge: `from_node` feeds `to_node`, via some item.

    The uniqueness key includes via_item and via_ref so two different notebooks
    writing the same target stay two distinct, separately-attributable edges.
    """

    __tablename__ = "lineage_edge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    from_node_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=False
    )
    to_node_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=False
    )

    via_item_id: Mapped[str | None] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="SET NULL"), nullable=True
    )
    # Which cell / activity inside that item, so the Code tab can highlight it.
    via_ref: Mapped[str] = mapped_column(String(200), default="")
    write_mode: Mapped[str] = mapped_column(
        String(30), default=""
    )  # overwrite/append/merge

    source: Mapped[str] = mapped_column(String(20), default=SOURCE_PARSED)
    confidence: Mapped[str] = mapped_column(String(10), default=CONF_HIGH)
    # A reference we could not statically resolve (an f-string table name, a
    # pipeline parameter). Kept WITH the unresolved expression so the UI can
    # show what it could not work out rather than silently dropping the edge.
    is_dynamic: Mapped[bool] = mapped_column(Boolean, default=False)
    dynamic_expr: Mapped[str] = mapped_column(Text, default="")

    detected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "from_node_id", "to_node_id", "via_item_id", "via_ref", name="uq_lineage_edge"
        ),
        Index("ix_edge_to", "tenant_id", "to_node_id"),
        Index("ix_edge_from", "tenant_id", "from_node_id"),
    )


class ColumnEdge(Base):
    """A column-level edge, carrying the transform that produced it."""

    __tablename__ = "column_edge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    from_column_id: Mapped[str] = mapped_column(
        String(700), ForeignKey("column_node.id", ondelete="CASCADE"), nullable=False
    )
    to_column_id: Mapped[str] = mapped_column(
        String(700), ForeignKey("column_node.id", ondelete="CASCADE"), nullable=False
    )

    transform_expr: Mapped[str] = mapped_column(Text, default="")
    via_item_id: Mapped[str | None] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="SET NULL"), nullable=True
    )
    via_ref: Mapped[str] = mapped_column(String(200), default="")

    source: Mapped[str] = mapped_column(String(20), default=SOURCE_PARSED)
    confidence: Mapped[str] = mapped_column(String(10), default=CONF_HIGH)
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "from_column_id",
            "to_column_id",
            "via_item_id",
            "via_ref",
            name="uq_column_edge",
        ),
        Index("ix_coledge_to", "tenant_id", "to_column_id"),
        Index("ix_coledge_from", "tenant_id", "from_column_id"),
    )


class ItemInvocation(Base):
    """
    One item running another: a pipeline's Notebook / Dataflow / Invoke-pipeline
    activity, `notebookutils.notebook.run`, `%run`. Parsed from code, replaced
    on every parse. What "Orchestrated by" is built from — never a guess.
    """

    __tablename__ = "item_invocation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    from_item_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_item_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("from_item_id", "to_item_id", name="uq_item_invocation"),
    )


class CodeArtifact(Base):
    """
    One part of an item definition, stored as text plus a hash.

    Keeping the raw text means a parser improvement re-runs over what we
    already hold instead of re-fetching every notebook in the tenant — which
    matters because getDefinition is an LRO with real rate limits.
    """

    __tablename__ = "code_artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=False
    )

    part_path: Mapped[str] = mapped_column(String(500), nullable=False)
    language: Mapped[str] = mapped_column(String(30), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)

    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    fetch_mode: Mapped[str] = mapped_column(String(10), default="api")  # api | git

    __table_args__ = (UniqueConstraint("item_id", "part_path", name="uq_artifact_part"),)


class ParseResult(Base):
    """
    What a parser made of one artifact.

    Stored separately from the edges so "we parsed this and found nothing" is
    distinguishable from "we never parsed this", and so the unresolved-lineage
    finding has something concrete to point at.
    """

    __tablename__ = "parse_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("code_artifact.id", ondelete="CASCADE"), nullable=False
    )

    parser: Mapped[str] = mapped_column(String(40), nullable=False)
    parser_version: Mapped[int] = mapped_column(Integer, default=1)
    artifact_sha256: Mapped[str] = mapped_column(String(64), default="")

    warnings: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(10), default=CONF_HIGH)
    edges_found: Mapped[int] = mapped_column(Integer, default=0)
    column_edges_found: Mapped[int] = mapped_column(Integer, default=0)
    parsed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("artifact_id", "parser", name="uq_parse_result"),)


class DeltaVersion(Base):
    """
    One Delta commit. Append-only: history is the evidence behind "who wrote
    this table and when", so a re-scan adds versions and never rewrites them.
    """

    __tablename__ = "delta_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=False
    )

    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    operation: Mapped[str] = mapped_column(String(60), default="")
    operation_parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    engine_info: Mapped[str] = mapped_column(String(200), default="")

    # Filled in by the job-run correlator when a commit timestamp lands inside
    # exactly one job run — the fallback when static parsing cannot say who wrote.
    job_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("job_run.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("node_id", "version", name="uq_delta_version"),
        Index("ix_delta_version_recent", "tenant_id", "committed_at"),
    )


class DeltaStats(Base):
    """Current size/shape of a Delta table. One row per node, overwritten."""

    __tablename__ = "delta_stats"

    node_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    current_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    num_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 1 when num_rows is known exactly (from a full load, tracked since);
    # 0 when it is the last known count and changes since could not be counted.
    rows_exact: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    num_files: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_write_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_optimize_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_vacuum_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class JobRun(Base):
    """A notebook / pipeline / dataflow execution."""

    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=False
    )

    run_guid: Mapped[str] = mapped_column(String(80), nullable=False)
    job_type: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(30), default="")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    invoked_by_item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("item_id", "run_guid", name="uq_job_run"),
        Index("ix_job_run_window", "tenant_id", "started_at", "ended_at"),
    )


class Principal(Base):
    """A user, group or service principal. id is `prn:{graphId or upn}`."""

    __tablename__ = "principal"

    id: Mapped[str] = mapped_column(String(400), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    graph_id: Mapped[str] = mapped_column(String(36), default="")
    upn: Mapped[str] = mapped_column(String(320), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(400), default="")
    principal_type: Mapped[str] = mapped_column(String(30), default="User")


class AccessGrant(Base):
    """
    A resolved "this principal can read this thing" fact.

    `path_description` is the sentence the Access tab shows — "Member of
    BI-Analysts, which has Viewer on Sales BI [Prod]" — because a permission
    without its path is unactionable.
    """

    __tablename__ = "access_grant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    principal_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str | None] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=True
    )
    item_id: Mapped[str | None] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=True
    )

    permission: Mapped[str] = mapped_column(String(40), default="")
    path_description: Mapped[str] = mapped_column(Text, default="")
    # True when the principal cannot touch the table directly but can see its
    # data through something else — a Direct Lake model on a fixed identity.
    is_indirect: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    resolved_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_grant_node", "tenant_id", "node_id"),)


class Doc(Base):
    """
    A description/owner/tags record for a node or a column.

    Several sources can describe the same thing; the resolver prefers a human
    edit over TMDL over a Delta table property (DOC_SOURCE_RANK). Rows are kept
    per source rather than merged so a user edit never destroys what the
    collector found.
    """

    __tablename__ = "doc"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    node_id: Mapped[str | None] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=True
    )
    column_id: Mapped[str | None] = mapped_column(
        String(700), ForeignKey("column_node.id", ondelete="CASCADE"), nullable=True
    )

    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(320), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(20), default=DOC_DELTA_PROPERTY)
    updated_by: Mapped[str] = mapped_column(String(320), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("node_id", "column_id", "source", name="uq_doc_target_source"),
    )


class Finding(Base):
    """A governance finding. Re-detection updates in place via (type, target)."""

    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    finding_type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    node_id: Mapped[str | None] = mapped_column(
        String(400), ForeignKey("data_node.id", ondelete="CASCADE"), nullable=True
    )
    item_id: Mapped[str | None] = mapped_column(
        String(120), ForeignKey("item.id", ondelete="CASCADE"), nullable=True
    )

    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)

    detected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "finding_type", "node_id", "item_id", name="uq_finding_target"
        ),
        Index("ix_finding_open", "tenant_id", "resolved_at", "severity"),
    )
