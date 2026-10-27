import { type RefObject, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Download, GitBranch, Search } from "lucide-react";
import { api } from "../api/client";
import type { CatalogRow, CollectorError, Overview } from "../api/types";
import { Card, Chip, LayerDot, SectionHead } from "../components/primitives";
import {
  groupCatalog,
  countLabel,
  isSingleRowType,
  isStorage,
  lineageOf,
  matchesSearch,
} from "../lib/catalog";
import { compact, relativeTime } from "../lib/format";

/**
 * The Data Catalog's landing page: two catalogs, the tables and the semantic
 * models and reports built on them, which answer different questions. The
 * filters above apply to both.
 */
export function DocsOverview({
  overview,
  catalog,
  lede,
  exportUrl,
  onPickNode,
  onOpenGraph,
}: {
  overview: Overview;
  catalog: CatalogRow[] | null;
  lede: string;
  /** Download link for the catalog as CSV, with the current filters. */
  exportUrl?: string;
  onPickNode: (nodeId: string, tab?: string) => void;
  /** Opens the lineage graph with a selector (a row's own lineage). */
  onOpenGraph: (selector: string) => void;
}) {
  const rows = useMemo(() => catalog ?? [], [catalog]);
  const tables = useMemo(() => rows.filter(isStorage), [rows]);
  const consumers = useMemo(
    () => rows.filter((r) => r.kind === "model_table" || r.kind === "report_visual"),
    [rows],
  );
  const tablesRef = useRef<HTMLElement>(null);
  const consumersRef = useRef<HTMLElement>(null);
  const openLineage = (row: CatalogRow) => onOpenGraph(lineageOf(row.id));

  return (
    <div className="stack">
      <div className="page-head">
        <h1 className="page-title">Overview</h1>
        <p className="page-lede">{lede}</p>
      </div>

      {overview.gaps > 0 && <GapsNote count={overview.gaps} />}

      <CatalogSection
        sectionRef={tablesRef}
        title="Tables"
        sub="Lakehouse, warehouse and mirrored tables: who writes each one, how, and when"
        rows={tables}
        total={tables.length}
        variant="storage"
        exportUrl={exportUrl}
        loading={catalog === null}
        onPickNode={onPickNode}
        onOpenLineage={openLineage}
      />

      {(catalog === null || consumers.length > 0) && (
        <CatalogSection
          sectionRef={consumersRef}
          title="Semantic models and reports"
          sub="What each model and report is built on"
          rows={consumers}
          total={consumers.length}
          variant="consumers"
          loading={catalog === null}
          onPickNode={onPickNode}
          onOpenLineage={openLineage}
        />
      )}
    </div>
  );
}

/** One catalog with its own search: the tables, or the models and reports. */
function CatalogSection({
  sectionRef,
  title,
  sub,
  rows,
  total,
  variant,
  exportUrl,
  loading,
  onPickNode,
  onOpenLineage,
}: {
  sectionRef: RefObject<HTMLElement>;
  title: string;
  sub: string;
  rows: CatalogRow[];
  total: number;
  variant: Variant;
  exportUrl?: string;
  loading: boolean;
  onPickNode: (nodeId: string, tab?: string) => void;
  onOpenLineage: (row: CatalogRow) => void;
}) {
  const [query, setQuery] = useState("");
  const shown = useMemo(() => rows.filter((r) => matchesSearch(r, query)), [rows, query]);
  return (
    <section className="stack-sm catalog-section" ref={sectionRef}>
      <SectionHead
        title={title}
        sub={sub}
        right={
          <span className="toolbar">
            <label className="catalog-search">
              <Search size={14} aria-hidden="true" />
              <input
                type="search"
                placeholder={variant === "storage" ? "Find a table, item or workspace" : "Find a model or report"}
                aria-label={`Find in ${title.toLowerCase()}`}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <Chip tone="blue">{shown.length === total ? `${total}` : `${shown.length} of ${total}`}</Chip>
            {exportUrl && (
              <a className="btn" href={exportUrl} download>
                <Download size={14} aria-hidden="true" /> Export CSV
              </a>
            )}
          </span>
        }
      />
      {loading ? (
        <p className="muted-note">Loading the catalog…</p>
      ) : (
        <CatalogTable
          // A new search opens fresh: everything that matches, unfolded.
          key={query.trim()}
          rows={shown}
          variant={variant}
          expandAll={Boolean(query.trim())}
          onPickNode={onPickNode}
          onOpenLineage={onOpenLineage}
        />
      )}
    </section>
  );
}

const GAP_REASON: Record<CollectorError["reason"], string> = {
  needs_contributor: "needs Contributor on the workspace",
  protected_label: "protected by a sensitivity label",
  rate_limited: "Fabric asked us to slow down; read next scan",
  unsupported: "not supported yet",
  error: "could not be read",
};

/** What the last scans could not read, so the numbers below are not taken as complete. */
function GapsNote({ count }: { count: number }) {
  const [gaps, setGaps] = useState<CollectorError[] | null>(null);
  const [open, setOpen] = useState(false);
  const toggle = () => {
    setOpen((v) => !v);
    if (gaps === null) {
      api
        .errors()
        .then(setGaps)
        .catch(() => setGaps([]));
    }
  };
  return (
    <div className="gaps-note">
      <p>
        <strong>{count === 1 ? "1 thing" : `${count} things`}</strong> could not be read in the
        last scans, so some lineage may be missing.{" "}
        <button type="button" className="link" onClick={toggle} aria-expanded={open}>
          {open ? "Hide" : "Show what"}
        </button>
      </p>
      {open &&
        (gaps === null ? (
          <p className="muted-note">Loading…</p>
        ) : (
          <ul className="gaps-list">
            {gaps.slice(0, 50).map((gap, i) => (
              <li key={`${gap.scopeId}:${gap.collector}:${i}`}>
                <strong>{gap.scopeName || gap.scope}</strong> — {GAP_REASON[gap.reason] ?? gap.message}
              </li>
            ))}
            {count > 50 && <li className="muted-note">and {count - 50} more</li>}
          </ul>
        ))}
    </div>
  );
}

type Variant = "storage" | "consumers";

const GRID: Record<Variant, string> = {
  storage: "minmax(0, 1.5fr) minmax(0, 1.4fr) 100px 100px 80px 44px",
  consumers: "minmax(0, 1.3fr) minmax(0, 1.8fr) 110px 100px 44px",
};
const HEAD: Record<Variant, string[]> = {
  storage: ["Name", "Written by", "Mode", "Last write", "Rows", ""],
  consumers: ["Name", "Built on", "Mode", "Feeds", ""],
};

function CatalogTable({
  rows,
  variant,
  expandAll,
  onPickNode,
  onOpenLineage,
}: {
  rows: CatalogRow[];
  variant: Variant;
  /** A search is on: open every group that matched. */
  expandAll: boolean;
  onPickNode: (nodeId: string, tab?: string) => void;
  onOpenLineage: (row: CatalogRow) => void;
}) {
  const grid = GRID[variant];
  const groups = useMemo(() => groupCatalog(rows), [rows]);
  // Collapsed by default past a handful of workspaces: a 49-workspace tenant
  // should open as a table of contents, not a 1,000-row wall.
  const [open, setOpen] = useState<Set<string>>(() => {
    if (groups.length > 3 && !expandAll) return new Set();
    // A report's visuals and a model's tables stay folded unless a search
    // asked for them: forty reports are hundreds of rows.
    return new Set(
      groups.flatMap((ws) => [
        ws.id,
        ...ws.types
          .filter((t) => expandAll || !isSingleRowType(t.type))
          .flatMap((t) => [t.id, ...t.items.map((i) => i.id)]),
      ]),
    );
  });
  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (rows.length === 0) {
    return <p className="muted-note">Nothing matches.</p>;
  }

  const chevron = (id: string) =>
    open.has(id) ? <ChevronDown size={14} /> : <ChevronRight size={14} />;
  const entry = (row: CatalogRow, indent: number) => (
    <CatalogEntry
      key={row.id}
      row={row}
      variant={variant}
      indent={indent}
      onPickNode={onPickNode}
      onOpenLineage={onOpenLineage}
    />
  );

  return (
    <Card>
      <div className="grid-head" style={{ gridTemplateColumns: grid }}>
        {HEAD[variant].map((label, i) => (
          <div key={i}>{label}</div>
        ))}
      </div>
      {groups.map((ws) => (
        <div key={ws.id}>
          <button
            type="button"
            className="grid-row catalog-ws"
            style={{ gridTemplateColumns: grid }}
            aria-expanded={open.has(ws.id)}
            onClick={() => toggle(ws.id)}
          >
            <span className="catalog-name">
              {chevron(ws.id)}
              <strong>{ws.name}</strong>
            </span>
            <span className="cell-muted">
              {ws.types.map((t) => countLabel(t.items.length, t.name)).join(" · ")}
            </span>
          </button>
          {open.has(ws.id) &&
            ws.types.map((group) => (
              <div key={group.id}>
                <button
                  type="button"
                  className="grid-row catalog-type"
                  style={{ gridTemplateColumns: grid }}
                  aria-expanded={open.has(group.id)}
                  onClick={() => toggle(group.id)}
                >
                  <span className="catalog-name" style={{ paddingLeft: 18 }}>
                    {chevron(group.id)}
                    {group.name}
                  </span>
                  <span className="cell-muted tnum">
                    {isSingleRowType(group.type)
                      ? group.items.length
                      : `${group.items.length} items · ${group.count} tables`}
                  </span>
                </button>
                {open.has(group.id) &&
                  (isSingleRowType(group.type)
                    ? group.items.flatMap((item) => item.rows).map((row) => entry(row, 38))
                    : group.items.map((item) => (
                        <div key={item.id}>
                          <button
                            type="button"
                            className="grid-row catalog-item"
                            style={{ gridTemplateColumns: grid }}
                            aria-expanded={open.has(item.id)}
                            onClick={() => toggle(item.id)}
                          >
                            <span className="catalog-name" style={{ paddingLeft: 38 }}>
                              {chevron(item.id)}
                              {item.name}
                            </span>
                            <span className="cell-muted tnum">{item.rows.length}</span>
                          </button>
                          {open.has(item.id) && item.rows.map((row) => entry(row, 58))}
                        </div>
                      )))}
              </div>
            ))}
        </div>
      ))}
    </Card>
  );
}

/**
 * One table, model or report. The row opens its page; the branch button at
 * the end opens its lineage in the graph.
 */
function CatalogEntry({
  row,
  variant,
  indent,
  onPickNode,
  onOpenLineage,
}: {
  row: CatalogRow;
  variant: Variant;
  indent: number;
  onPickNode: (nodeId: string, tab?: string) => void;
  onOpenLineage: (row: CatalogRow) => void;
}) {
  const open = () => onPickNode(row.id);
  const lineage = (
    <span className="catalog-lineage">
      <button
        type="button"
        className="btn btn-icon btn-sm"
        aria-label={`Lineage of ${row.name}`}
        title="Open its lineage in the graph"
        onClick={(e) => {
          e.stopPropagation();
          onOpenLineage(row);
        }}
      >
        <GitBranch size={14} aria-hidden="true" />
      </button>
    </span>
  );
  const name = (
    <span className="catalog-name cell-mono" style={{ paddingLeft: indent }}>
      <LayerDot layer={row.layer} />
      {row.schema ? `${row.schema}.${row.name}` : row.name}
    </span>
  );
  return (
    <div
      role="button"
      tabIndex={0}
      className="grid-row catalog-entry"
      style={{ gridTemplateColumns: GRID[variant] }}
      onClick={open}
      onKeyDown={(e) => {
        if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          open();
        }
      }}
      title={
        variant === "storage"
          ? row.producers.map((p) => `${p.kind}: ${p.item}`).join("\n")
          : row.sources.join("\n")
      }
    >
      {name}
      {variant === "storage" ? (
        <>
          <span className="cell-muted catalog-producer">
            {row.producers.length === 0 ? (
              row.producerEvidence === "delta" ? (
                <>
                  <Chip tone="neutral">{row.producerKind}</Chip>
                  <span
                    className="ellipsis"
                    title="The Delta log shows this engine wrote the table, but no current item's code names it as a target."
                  >
                    per Delta log · item not identified
                  </span>
                </>
              ) : (
                <Chip tone="amber">Unknown</Chip>
              )
            ) : (
              <>
                <Chip tone="blue">{row.producerKind}</Chip>
                <span className="ellipsis">{row.producers.map((p) => p.item).join(", ")}</span>
              </>
            )}
          </span>
          <span className="cell-muted">{row.producers[0]?.mode || "—"}</span>
          <span className="cell-muted tnum">{relativeTime(row.lastWriteAt)}</span>
          <span className="cell-muted tnum">{row.rows != null ? compact(row.rows) : "—"}</span>
        </>
      ) : (
        <>
          <span className="cell-muted ellipsis cell-mono">
            {row.sources.length ? row.sources.join(", ") : "—"}
          </span>
          <span className="cell-muted">
            {row.kind === "model_table" ? row.storageMode || "—" : "Report"}
          </span>
          <span className="cell-muted tnum">
            {row.kind === "model_table" ? countLabel(row.reports, "Reports") : "—"}
          </span>
        </>
      )}
      {lineage}
    </div>
  );
}
