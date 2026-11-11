import { useMemo, useState } from "react";
import { AlertTriangle, CalendarClock, GitFork, Info } from "lucide-react";
import type { GraphEdge, GraphNode, GraphPayload, Layer, NodeKind } from "../../api/types";
import type { JobRun, JobSummary, JobsPayload, LivePayload, RunStatus, RunsPayload } from "../api";
import { DataTable, type Column } from "../../audience/components/DataTable";
import { Empty, Loading } from "../../components/primitives";
import { LineageGraph } from "../../views/LineageGraph";
import { OpenInFabric } from "../../components/OpenInFabric";
import { StatusBadge } from "../components/Status";
import { TypeIcon } from "../components/TypeIcon";
import { dataNeighbours, dependencyChecks, descendantsOf, type Check } from "../deps";
import { STATUS_LABEL, duration, relative, when } from "../lib";
import { useJob, useNow } from "../store";
import { GraphTab } from "./JobDetail";

/** Lanes, left to right: what starts things, then the order data moves in. */
const LANES = ["trigger", "pipeline", "ingest", "compute", "model"] as const;
const LANE_LABEL: Record<(typeof LANES)[number], string> = {
  trigger: "Schedule",
  pipeline: "Pipeline",
  ingest: "Dataflow · Copy job",
  compute: "Notebook · Spark job",
  model: "Semantic model",
};
const laneOf = (type: string): (typeof LANES)[number] =>
  type === "DataPipeline"
    ? "pipeline"
    : type === "Dataflow" || type === "CopyJob"
      ? "ingest"
      : type === "SemanticModel"
        ? "model"
        : "compute";

type Show = "both" | "runs" | "data";

/**
 * What triggers what, and what waits for whose data — the Data Catalog's
 * lineage, for items instead of tables. Solid lines: a schedule or an item
 * starts another. Dashed: an item reads a table another item writes. Click an
 * item to trace everything upstream and downstream of it.
 */
export function Chains({
  all,
  jobs,
  runs,
  live,
  onOpen,
}: {
  all: JobsPayload;
  jobs: JobsPayload;
  runs: RunsPayload | null;
  live: LivePayload | null;
  onOpen: (id: string, runId?: string) => void;
}) {
  const now = useNow(15_000);
  const running = useMemo(() => new Set((live?.running ?? []).map((r) => r.item.id)), [live]);
  const byId = useMemo(() => new Map(all.jobs.map((j) => [j.id, j])), [all]);

  // A chain starts at an item that runs others and that nothing runs.
  const roots = useMemo(
    () =>
      all.jobs
        .filter((j) => j.children.length)
        .sort(
          (a, b) =>
            Number(a.parents.some((p) => byId.has(p.id))) - Number(b.parents.some((p) => byId.has(p.id))) ||
            descendantsOf(b.id, byId).size - descendantsOf(a.id, byId).size ||
            a.name.localeCompare(b.name),
        ),
    [all, byId],
  );
  const [rootId, setRootId] = useState<string | null>(null);
  const root = (rootId && roots.find((r) => r.id === rootId)) || roots[0] || null;

  const checks = useMemo(
    () => dependencyChecks(all, runs?.runs ?? [], now - 30 * 86_400_000),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [all, runs, Math.floor(now / 60_000)],
  );

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Execution chains</h2>
        <p className="page-head-sub">
          What calls what. Pick a chain — a pipeline, or a notebook that runs notebooks with{" "}
          <code>notebook.run</code>, <code>runMultiple</code> or <code>%run</code> — to see its latest run as a graph: every
          activity, what it ran, how long it took. Click a box for its details.
        </p>
      </header>

      {!roots.length ? (
        <Empty title="No chains">
          Nothing here runs another item. A chain appears when a pipeline runs notebooks, dataflows or pipelines, or a
          notebook runs notebooks.
        </Empty>
      ) : (
        <>
          <div className="mon-chain-picker" role="tablist" aria-label="Chains">
            {roots.map((r) => {
              const size = descendantsOf(r.id, byId).size;
              const st: RunStatus | null = running.has(r.id) ? "running" : r.latest?.status ?? null;
              return (
                <button
                  key={r.id}
                  type="button"
                  role="tab"
                  aria-selected={r.id === root?.id}
                  className={`mon-chain-card${r.id === root?.id ? " active" : ""}`}
                  onClick={() => setRootId(r.id)}
                >
                  <span className="mon-chain-card-head">
                    <TypeIcon type={r.type} />
                    <strong>{r.name}</strong>
                    {st && <StatusBadge status={st} compact />}
                  </span>
                  <span className="muted-note">
                    {r.parents.length ? `inside ${r.parents.map((p) => p.name).join(", ")}` : r.schedule ? r.schedule.text : "Started by hand or the API"}
                  </span>
                  <span className="muted-note">
                    runs {size} item{size === 1 ? "" : "s"} · usual {duration(r.stats.avgSec)}
                    {r.latest ? ` · last ${relative(r.latest.startedAt, now)}` : ""}
                  </span>
                </button>
              );
            })}
          </div>
          {root && (
            <section className="mon-chain-head">
              <TypeIcon type={root.type} size={18} />
              <div className="mon-chain-head-name">
                <h3>{root.name}</h3>
                <span className="muted-note">
                  {root.typeLabel} · {root.workspace}
                </span>
              </div>
              <span className="mon-head-spacer" />
              <button type="button" className="btn" onClick={() => onOpen(root.id)}>
                Open item
              </button>
              <OpenInFabric link={root} />
            </section>
          )}
          {root && <ChainGraph key={root.id} root={root} onOpen={onOpen} />}
        </>
      )}

      <section className="card mon-pad">
        <div className="mon-section-head">
          <h3>
            <AlertTriangle size={15} aria-hidden="true" className="mon-icon-warn" /> Dependency checks, every chain, last 30 days
          </h3>
          <p className="muted-note">
            An item that reads a table another item writes depends on it. These are the dependencies that nothing orders,
            the runs that read unfinished or failed data, and items started twice.
          </p>
        </div>
        {checks.length ? (
          <ul className="mon-checks">
            {checks.map((c, i) => (
              <CheckRow key={i} check={c} onOpen={onOpen} />
            ))}
          </ul>
        ) : (
          <p className="muted-note">
            <Info size={13} aria-hidden="true" /> Every data dependency is ordered, and no run read unfinished or failed input.
          </p>
        )}
      </section>

      <details className="card mon-estate">
        <summary>
          <GitFork size={15} aria-hidden="true" /> The whole estate: every item, what starts it and whose data it reads
        </summary>
        <EstateGraph all={all} jobs={all} running={running} now={now} onOpen={onOpen} />
      </details>
    </div>
  );
}

/**
 * One chain as the item page's Graph: every activity of one run and what it
 * ran, with the run picker and a side panel for the picked task.
 */
function ChainGraph({ root, onOpen }: { root: JobSummary; onOpen: (id: string, runId?: string) => void }) {
  const { page, error } = useJob(root.id);
  const [runId, setRunId] = useState<string | null>(null);
  if (error) return <p className="banner banner-danger">{error}</p>;
  if (!page) return <Loading what="the chain" />;
  // The latest run, running or not; the run picker reaches older ones.
  const run: JobRun | null = page.runs.find((r) => r.runId === runId) ?? page.runs[0] ?? null;
  return <GraphTab page={page} run={run} onRun={setRunId} onOpen={onOpen} />;
}

/** Every item as the catalog's lineage graph, and the dependency table. */
function EstateGraph({
  all,
  jobs,
  running,
  now,
  onOpen,
}: {
  all: JobsPayload;
  jobs: JobsPayload;
  running: Set<string>;
  now: number;
  onOpen: (id: string, runId?: string) => void;
}) {
  const [show, setShow] = useState<Show>("both");
  const byId = useMemo(() => new Map(all.jobs.map((j) => [j.id, j])), [all]);
  const shown = useMemo(() => {
    const keep = new Set<string>();
    for (const j of jobs.jobs) {
      keep.add(j.id);
      descendantsOf(j.id, byId).forEach((d) => keep.add(d));
    }
    return all.jobs.filter((j) => keep.has(j.id));
  }, [jobs, all, byId]);
  const shownIds = useMemo(() => new Set(shown.map((j) => j.id)), [shown]);
  const neighbours = useMemo(() => dataNeighbours(all.data), [all]);

  const payload = useMemo<GraphPayload>(() => {
    const nodes: GraphNode[] = [];
    const edges: GraphEdge[] = [];
    const node = (id: string, name: string, lane: string, sub: string, workspace: string, path: string): GraphNode => ({
      id,
      name,
      shortName: name,
      sub,
      layer: lane as Layer,
      layerLabel: LANE_LABEL[lane as keyof typeof LANE_LABEL],
      kind: "external" as NodeKind,
      kindLabel: "",
      path,
      workspace,
      isFocus: false,
      onPath: false,
    });
    const edge = (from: string, to: string, via: string, dashed: boolean): GraphEdge => ({
      from,
      to,
      viaItem: via,
      viaRef: "",
      writeMode: "",
      source: "parsed",
      confidence: "high",
      isDynamic: dashed,
      onPath: false,
    });
    for (const j of shown) {
      nodes.push(node(j.id, j.name, laneOf(j.type), j.typeLabel, j.workspace, `${j.workspace} / ${j.name}`));
      if (show !== "data") {
        if (j.schedule) {
          const sid = `schedule:${j.id}`;
          nodes.push(node(sid, j.schedule.text, "trigger", j.schedule.enabled ? "Schedule" : "Schedule (off)", "", `Starts ${j.name}`));
          edges.push(edge(sid, j.id, "Schedule", false));
        }
        for (const c of j.children) if (shownIds.has(c.id)) edges.push(edge(j.id, c.id, c.via, false));
      }
    }
    if (show !== "runs") {
      for (const d of all.data ?? []) {
        if (shownIds.has(d.from) && shownIds.has(d.to)) edges.push(edge(d.from, d.to, d.tables.join(", "), true));
      }
    }
    const lanes = LANES.map((l) => ({ layer: l as Layer, label: LANE_LABEL[l], nodeIds: nodes.filter((n) => (n.layer as string) === l).map((n) => n.id) }));
    return { focus: null, lanes, nodes, edges };
  }, [shown, shownIds, all, show]);

  const columns: Column<JobSummary>[] = [
    {
      key: "item",
      label: "Item",
      value: (j) => j.name.toLowerCase(),
      render: (j) => (
        <span className="mon-name-cell">
          <TypeIcon type={j.type} />
          <span className="strong link-like">{j.name}</span>
        </span>
      ),
    },
    {
      key: "trigger",
      label: "Started by",
      value: (j) => j.parents.map((p) => p.name).join(", ") || j.schedule?.text || "",
      render: (j) => (
        <span className="mon-deps-cell">
          {j.schedule && (
            <span className={j.schedule.enabled ? "" : "muted-note"}>
              <CalendarClock size={12} aria-hidden="true" /> {j.schedule.text}
              {!j.schedule.enabled && " (off)"}
            </span>
          )}
          {j.parents.map((p) => (
            <ItemLink key={p.id} id={p.id} name={p.name} onOpen={onOpen} />
          ))}
          {!j.schedule && !j.parents.length && <span className="muted-note">By hand or the API</span>}
        </span>
      ),
    },
    {
      key: "runs",
      label: "Starts",
      value: (j) => j.children.length,
      render: (j) =>
        j.children.length ? (
          <span className="mon-deps-cell">
            {j.children.map((c) => (
              <span key={c.id}>
                <ItemLink id={c.id} name={c.name} onOpen={onOpen} /> <span className="muted-note">{c.via}</span>
              </span>
            ))}
          </span>
        ) : (
          <span className="muted-note">—</span>
        ),
    },
    {
      key: "waits",
      label: "Reads data from",
      value: (j) => neighbours.upstream.get(j.id)?.length ?? 0,
      render: (j) => <DepList deps={neighbours.upstream.get(j.id)} side="from" byId={byId} onOpen={onOpen} />,
    },
    {
      key: "feeds",
      label: "Feeds data to",
      value: (j) => neighbours.downstream.get(j.id)?.length ?? 0,
      render: (j) => <DepList deps={neighbours.downstream.get(j.id)} side="to" byId={byId} onOpen={onOpen} />,
    },
  ];

  return (
    <div className="stack mon-pad">
      <div className="mon-toolbar">
        <p className="muted-note mon-count">
          Solid: a schedule or an item starts another. Dashed: an item reads a table another writes. Click an item to trace
          its whole lineage.
        </p>
        <div className="segmented" role="group" aria-label="Dependencies">
          {(
            [
              ["both", "Runs and data"],
              ["runs", "Runs"],
              ["data", "Data"],
            ] as const
          ).map(([id, label]) => (
            <button key={id} type="button" className={show === id ? "active" : ""} onClick={() => setShow(id)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <LineageGraph
        payload={payload}
        order={LANES}
        layoutKey={`monitor-chains:${show}`}
        hint="Click an item to trace what triggers it and what it feeds · drag to rearrange · drag the background to pan"
        openLabel="Open item"
        onPick={(id) => onOpen(id.startsWith("schedule:") ? id.slice(9) : id)}
        nodeClassName={(n) => ((n.layer as string) === "trigger" ? "mon-gnode mon-gnode-trigger" : "mon-gnode")}
        nodeTitle={(n) => n.path}
        renderNode={(n) => <ChainCard node={n} job={byId.get(n.id)} running={running.has(n.id)} now={now} />}
      />
      <DataTable columns={columns} rows={shown} rowKey={(j) => j.id} onRowClick={(j) => onOpen(j.id)} exportName="job-monitor-dependencies" />
    </div>
  );
}

function ItemLink({ id, name, onOpen }: { id: string; name: string; onOpen: (id: string) => void }) {
  return (
    <button
      type="button"
      className="link-like"
      onClick={(e) => {
        e.stopPropagation();
        onOpen(id);
      }}
    >
      {name}
    </button>
  );
}

function DepList({
  deps,
  side,
  byId,
  onOpen,
}: {
  deps: { from: string; to: string; tables: string[] }[] | undefined;
  side: "from" | "to";
  byId: Map<string, JobSummary>;
  onOpen: (id: string) => void;
}) {
  if (!deps?.length) return <span className="muted-note">—</span>;
  return (
    <span className="mon-deps-cell">
      {deps.map((d) => {
        const other = byId.get(d[side]);
        return (
          <span key={d[side]} title={d.tables.join("\n")}>
            <ItemLink id={d[side]} name={other?.name ?? d[side]} onOpen={onOpen} />{" "}
            <span className="muted-note">
              {d.tables.length === 1 ? d.tables[0].split(" · ").pop() : `${d.tables.length} tables`}
            </span>
          </span>
        );
      })}
    </span>
  );
}

function CheckRow({
  check: c,
  onOpen,
  compact = false,
}: {
  check: Check;
  onOpen: (id: string, runId?: string) => void;
  compact?: boolean;
}) {
  const tone = c.kind === "after-failure" || c.kind === "overlap" ? "fail" : "warn";
  const title = {
    "after-failure": "Ran on failed input",
    overlap: "Started before its input was ready",
    unordered: "Not ordered",
    twice: "Started twice",
  }[c.kind];
  return (
    <li className={`mon-check-row mon-check-${tone}${compact ? " compact" : ""}`}>
      <span className="mon-check-kind">{title}</span>
      <span className="mon-check-main">
        <span>{c.text}</span>
        {c.tables && <span className="muted-note">Via {c.tables.join(", ")}</span>}
        {c.example && (
          <span className="muted-note">
            For example: {c.example.run.item.name} at{" "}
            <button type="button" className="link-like" onClick={() => onOpen(c.example!.run.item.id, c.example!.run.runId)}>
              {when(c.example.run.startedAt).slice(0, 16)}
            </button>
            , {c.example.upstreamRun.item.name}{" "}
            <button
              type="button"
              className="link-like"
              onClick={() => onOpen(c.example!.upstreamRun.item.id, c.example!.upstreamRun.runId)}
            >
              {c.example.upstreamRun.status === "failed"
                ? `failed at ${when(c.example.upstreamRun.endedAt).slice(11, 16)}`
                : c.example.upstreamRun.endedAt
                  ? `finished at ${when(c.example.upstreamRun.endedAt).slice(11, 16)}`
                  : "still running"}
            </button>
            .
          </span>
        )}
        <span className="muted-note">
          {c.kind === "unordered" || c.kind === "overlap"
            ? `Fix: run ${c.item.name} from the same pipeline, after ${c.upstream?.name} (an activity with "on success"), instead of on its own schedule.`
            : c.kind === "after-failure"
              ? `Fix: start ${c.item.name} only on ${c.upstream?.name}'s success, or check its input first.`
              : `Fix: drop the schedule, or the orchestrator's activity — one start is enough.`}
        </span>
      </span>
      {!compact && (
        <button type="button" className="btn btn-sm" onClick={() => onOpen(c.item.id)}>
          Open {c.item.name}
        </button>
      )}
    </li>
  );
}

/** A card in the chain graph: status, type, usual duration, last run. */
function ChainCard({ node, job, running, now }: { node: GraphNode; job: JobSummary | undefined; running: boolean; now: number }) {
  if (!job) {
    return (
      <>
        <span className="name">
          <CalendarClock size={12} aria-hidden="true" /> {node.name}
        </span>
        <span className="sub">{node.sub}</span>
      </>
    );
  }
  const status: RunStatus | null = running ? "running" : job.latest?.status ?? null;
  return (
    <>
      <span className="name">
        <span className={`mon-dot mon-${status ?? "unknown"}${running ? " mon-pulse" : ""}`} aria-hidden="true" />
        {job.name}
      </span>
      <span className="sub">
        {job.typeLabel}
        {job.stats.avgSec != null ? ` · usual ${duration(job.stats.avgSec)}` : ""}
      </span>
      <span className="ws">
        {running
          ? "running now"
          : job.latest
            ? `${STATUS_LABEL[job.latest.status]} ${relative(job.latest.startedAt, now)}`
            : job.parents.some((p) => p.type === "Notebook")
              ? "in its caller's session"
              : "never run"}
      </span>
    </>
  );
}

