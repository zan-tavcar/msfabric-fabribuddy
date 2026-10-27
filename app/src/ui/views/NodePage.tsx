import { useEffect, useMemo, useRef, useState } from "react";
import { GitBranch, Info } from "lucide-react";
import { OpenInFabric } from "../components/OpenInFabric";
import { ApiError, api } from "../api/client";
import type {
  AccessPayload,
  CodePayload,
  Column,
  GraphPayload,
  NodePage as NodePayload,
} from "../api/types";
import { Card, Chip, LayerChip, Loading } from "../components/primitives";
import { DescriptionEditor } from "../components/DescriptionEditor";
import { lineageOf } from "../lib/catalog";
import { permissionTone } from "../lib/layers";
import { GraphLegend, LineageGraph } from "./LineageGraph";
import { PowerBiAccess } from "../audience/views/Access";

const TABS = [
  ["overview", "Overview"],
  ["columns", "Columns"],
  ["lineage", "Lineage"],
  ["code", "Code"],
  ["access", "Access"],
] as const;

export type TabId = (typeof TABS)[number][0];

/**
 * A report or a semantic model is Power BI content: its code and access are
 * Report Audience's subject. A report keeps Overview and Lineage; a model also
 * keeps the tables it uses. A report's Access tab shows Report Audience's
 * resolved access in place; a model's lists its grants like a table's.
 */
function tabsFor(kind: string): readonly TabId[] {
  if (kind === "report_visual") return ["overview", "lineage", "access"];
  if (kind === "model_table") return ["overview", "columns", "lineage", "access"];
  return TABS.map(([id]) => id);
}

/** A report's GUID from its node id (`rpt:{ws}:{report}:report`). */
function itemGuid(nodeId: string, prefix: "rpt"): string | null {
  const parts = nodeId.split(":");
  return parts[0] === prefix && parts.length >= 3 ? parts[2] : null;
}

export function NodePage({
  nodeId,
  tab,
  onTab,
  onPickNode,
  onOpenGraph,
  canEdit = false,
}: {
  nodeId: string;
  tab: TabId;
  onTab: (tab: TabId) => void;
  onPickNode: (nodeId: string, tab?: TabId) => void;
  /** Opens the lineage graph centred on this node: what feeds it and what it feeds. */
  onOpenGraph?: (selector: string) => void;
  /** A contributor or admin: may write table and column descriptions. */
  canEdit?: boolean;
}) {
  const [page, setPage] = useState<NodePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setError(null);
    api
      .node(nodeId)
      .then((p) => !cancelled && setPage(p))
      .catch((e) => !cancelled && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (error) return <p className="banner banner-danger">{error}</p>;
  if (!page) return <Loading what="this item" />;
  const tabs = tabsFor(page.kind);
  const shown: TabId = tabs.includes(tab) ? tab : "overview";
  // A report's access is Report Audience's resolved view (report AND model);
  // a semantic model's is read directly, like a table's.
  const reportAccess = page.kind === "report_visual";

  return (
    <div className="stack">
      <div className="node-head">
        <div className="node-path">{page.path}</div>
        <div className="node-title-row">
          <h1 className="node-title">{page.shortName}</h1>
          <LayerChip
            layer={page.layer}
            label={page.layerIsDeclared ? page.layerLabel : `${page.layerLabel} (inferred)`}
          />
          {page.kindLabel !== page.layerLabel && <Chip tone="blue">{page.kindLabel}</Chip>}
          {onOpenGraph && (
            <button
              type="button"
              className="btn node-graph-btn"
              onClick={() => onOpenGraph(lineageOf(nodeId))}
              title="Everything that feeds this table and everything it feeds, in the lineage graph"
            >
              <GitBranch size={14} aria-hidden="true" /> Open in graph
            </button>
          )}
          <OpenInFabric link={page} />
        </div>
        <div className="page-lede">
          <DescriptionEditor
            label={page.shortName}
            text={page.description}
            hasText={page.hasDescription}
            meta={page.descriptionMeta}
            canEdit={canEdit}
            onSave={async (text) => setPage(await api.setNodeDescription(nodeId, text))}
          />
        </div>
      </div>

      <nav className="tabs" aria-label="Item sections">
        {TABS.filter(([id]) => tabs.includes(id)).map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={shown === id ? "tab active" : "tab"}
            onClick={() => onTab(id)}
          >
            {/* A semantic model's rows are its tables, each with its source. */}
            {id === "columns" && page.kind === "model_table" ? "Tables" : label}
          </button>
        ))}
      </nav>

      {shown === "overview" && <OverviewTab page={page} onPickNode={onPickNode} />}
      {shown === "columns" && <ColumnsTab nodeId={nodeId} canEdit={canEdit} />}
      {shown === "lineage" && <LineageTab nodeId={nodeId} onPickNode={onPickNode} />}
      {shown === "code" && <CodeTab nodeId={nodeId} />}
      {shown === "access" &&
        (reportAccess ? (
          <PowerBiAccess reportId={itemGuid(nodeId, "rpt")} />
        ) : (
          <AccessTab nodeId={nodeId} />
        ))}
    </div>
  );
}

/* ------------------------------------------------------------------ overview */

function OverviewTab({
  page,
  onPickNode,
}: {
  page: NodePayload;
  onPickNode: (nodeId: string) => void;
}) {
  return (
    <div className="detail-grid">
      <Card head="Details">
        {page.details.length === 0 ? (
          <p className="list-empty">
            No details collected yet. The Delta collector fills these in.
          </p>
        ) : (
          page.details.map((d) => (
            <div key={d.key} className="detail-row">
              <div className="k">{d.key}</div>
              <div className="v">{d.value}</div>
            </div>
          ))
        )}
      </Card>

      <div className="stack-sm">
        <Card head="Upstream · depends on">
          {page.upstream.length === 0 ? (
            <p className="list-empty">Origin. This is a source.</p>
          ) : (
            page.upstream.map((n) => (
              <button
                key={n.id}
                type="button"
                className="list-row"
                onClick={() => onPickNode(n.id)}
              >
                <span className={`layer-dot layer-${n.layer}`} aria-hidden="true" />
                {n.name}
              </button>
            ))
          )}
        </Card>

        <Card head="Downstream · used by">
          {page.downstream.length === 0 ? (
            <p className="list-empty">No consumers found.</p>
          ) : (
            page.downstream.map((n) => (
              <button
                key={n.id}
                type="button"
                className="list-row"
                onClick={() => onPickNode(n.id)}
              >
                <span className={`layer-dot layer-${n.layer}`} aria-hidden="true" />
                {n.name}
              </button>
            ))
          )}
        </Card>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- columns */

const COLUMN_GRID = "230px 170px minmax(0, 1fr)";

/** The schema. Lineage is table-level: no per-column trace. */
function ColumnsTab({ nodeId, canEdit }: { nodeId: string; canEdit: boolean }) {
  const [columns, setColumns] = useState<Column[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    setColumns(null);
    api
      .columns(nodeId)
      .then((r) => !cancelled && setColumns(r.columns))
      .catch(() => !cancelled && setColumns([]));
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (!columns) return <Loading what="columns" />;
  if (columns.length === 0) {
    return (
      <Card>
        <p className="list-empty">
          No columns collected. Lakehouse schemas come from the OneLake table API and the Delta log.
        </p>
      </Card>
    );
  }

  return (
    <Card>
      <div className="grid-head" style={{ gridTemplateColumns: COLUMN_GRID }}>
        <div>Column</div>
        <div>Type</div>
        <div>Description</div>
      </div>
      {columns.map((column) => (
        <div key={column.id} className="grid-row" style={{ gridTemplateColumns: COLUMN_GRID }}>
          <div className="cell-mono cell-strong">{column.name}</div>
          <div className="cell-mono cell-muted">
            {column.type}
            {column.isMeasure && (
              <>
                {" "}
                <Chip tone="purple">measure</Chip>
              </>
            )}
          </div>
          <div>
            <DescriptionEditor
              compact
              label={column.name}
              text={column.description}
              hasText={column.hasDescription}
              meta={column.descriptionMeta}
              canEdit={canEdit}
              onSave={async (text) =>
                setColumns((await api.setColumnDescription(nodeId, column.name, text)).columns)
              }
            />
          </div>
        </div>
      ))}
    </Card>
  );
}

/* ------------------------------------------------------------------- lineage */

function LineageTab({
  nodeId,
  onPickNode,
}: {
  nodeId: string;
  onPickNode: (nodeId: string) => void;
}) {
  const [graph, setGraph] = useState<GraphPayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Generous depths: this view is about the whole path through the node, not a
    // bounded neighbourhood, and the backend marks which nodes are on it.
    api
      .graph({ focus: nodeId, up: 25, down: 25 })
      .then((g) => !cancelled && setGraph(g))
      .catch(() => !cancelled && setGraph(null));
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  return (
    <div className="stack-sm">
      <div className="toolbar">
        <span className="muted-note">Which items feed this one, and what depends on it.</span>
        <GraphLegend />
      </div>
      {graph ? (
        <LineageGraph payload={graph} onPick={onPickNode} dimOffPath layoutKey={`node:${nodeId}`} />
      ) : (
        <Loading what="the graph" />
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------- code */

function CodeTab({ nodeId }: { nodeId: string }) {
  const [code, setCode] = useState<CodePayload | null>(null);
  const [pick, setPick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setCode(null);
    setPick(0);
    api
      .code(nodeId)
      .then((c) => !cancelled && setCode(c))
      .catch(() => !cancelled && setCode({ available: false, reason: "Could not load the code." }));
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (!code) return <Loading what="code" />;

  if (!code.available) {
    return (
      <Card>
        <div className="card-body" style={{ display: "flex", gap: 10 }}>
          <Info size={16} strokeWidth={2.2} aria-hidden="true" color="var(--muted)" />
          <div>
            <strong>Code unavailable</strong>
            <p className="muted-note" style={{ marginTop: 4 }}>
              {code.reason}
            </p>
          </div>
        </div>
      </Card>
    );
  }

  const definitions = code.definitions?.length ? code.definitions : [code];
  const shown = definitions[Math.min(pick, definitions.length - 1)];

  return (
    <div className="stack-sm">
      {definitions.length > 1 && (
        <div className="toolbar" role="tablist" aria-label="Writers of this table">
          <span className="muted-note">Written by {definitions.length} items:</span>
          {definitions.map((d, i) => (
            <button
              key={`${d.definedIn}-${i}`}
              type="button"
              role="tab"
              aria-selected={i === pick}
              className={i === pick ? "btn btn-primary" : "btn"}
              onClick={() => setPick(i)}
            >
              {d.definedIn}
            </button>
          ))}
        </div>
      )}
      <div className="code-meta">
        <div className="card">
          <div className="label">Defined in</div>
          <div className="value">{shown.definedIn}</div>
        </div>
        <div className="card">
          <div className="label">Write mode</div>
          <div className="value">{shown.writeMode || "—"}</div>
        </div>
        <div className="card">
          <div className="label">Lineage detected via</div>
          <div className="value green">{shown.detectedVia}</div>
        </div>
      </div>
      <CodeBlock text={shown.text ?? ""} highlight={shown.highlight ?? null} />
    </div>
  );
}

/**
 * The artifact with its writing block tinted and the writing line(s) marked,
 * scrolled so the write is in view — a 400-line notebook is useless if the
 * reader has to hunt for cell 37.
 */
export function CodeBlock({
  text,
  highlight,
}: {
  text: string;
  highlight: { startLine: number; endLine: number; lines?: number[] } | null;
}) {
  const lines = useMemo(() => text.split("\n"), [text]);
  const target = useRef<HTMLSpanElement | null>(null);
  const exact = useMemo(() => new Set(highlight?.lines ?? []), [highlight]);
  const anchor = highlight ? (highlight.lines?.[0] ?? highlight.startLine) : 0;

  useEffect(() => {
    target.current?.scrollIntoView({ block: "center" });
  }, [anchor, text]);

  if (!highlight) {
    return <pre className="code-block">{text}</pre>;
  }
  return (
    <pre className="code-block">
      {lines.map((line, index) => {
        const number = index + 1;
        const inBlock = number >= highlight.startLine && number <= highlight.endLine;
        const classes = ["code-line", inBlock ? "hit" : "", exact.has(number) ? "write" : ""]
          .filter(Boolean)
          .join(" ");
        return (
          <span key={number} ref={number === anchor ? target : undefined} className={classes}>
            <span className="code-num" aria-hidden="true">
              {number}
            </span>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

/* -------------------------------------------------------------------- access */

const ACCESS_GRID = "230px 130px minmax(0, 1fr) 130px 220px";

function AccessTab({ nodeId }: { nodeId: string }) {
  const [access, setAccess] = useState<AccessPayload | null>(null);
  // Set when the plan does not include access resolution (402): the reason
  // names the plan that does, which beats an empty table.
  const [locked, setLocked] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setAccess(null);
    setLocked(null);
    api
      .access(nodeId)
      .then((a) => !cancelled && setAccess(a))
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 402) setLocked(e.message);
        else setAccess({ summary: "", rows: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (locked) {
    return (
      <Card>
        <div className="card-body" style={{ display: "flex", gap: 10 }}>
          <Info size={16} strokeWidth={2.2} aria-hidden="true" color="var(--muted)" />
          <div>
            <strong>Not on your plan</strong>
            <p className="muted-note" style={{ marginTop: 4 }}>
              {locked} An admin can change the plan under Settings → Plan.
            </p>
          </div>
        </div>
      </Card>
    );
  }
  if (!access) return <Loading what="access" />;

  return (
    <div className="stack-sm">
      <p className="muted-note">{access.summary}</p>
      <Card>
        <div className="grid-head" style={{ gridTemplateColumns: ACCESS_GRID }}>
          <div>Principal</div>
          <div>Type</div>
          <div>Access path</div>
          <div>Permission</div>
          <div>Notes</div>
        </div>
        {access.rows.length === 0 ? (
          <p className="list-empty">
            No resolved access. Run the access collector to populate this.
          </p>
        ) : (
          access.rows.map((row, index) => (
            <div
              key={`${row.principal}-${index}`}
              className="grid-row"
              style={{ gridTemplateColumns: ACCESS_GRID }}
            >
              <div className="cell-strong">{row.principal}</div>
              <div className="cell-muted">{row.principalType}</div>
              <div className="cell-mono" style={{ fontSize: 12 }}>
                {row.path}
              </div>
              <div>
                <Chip tone={permissionTone(row.permission)}>{row.permission}</Chip>
              </div>
              <div>{row.note && <Chip tone="amber">{row.note}</Chip>}</div>
            </div>
          ))
        )}
      </Card>
    </div>
  );
}
