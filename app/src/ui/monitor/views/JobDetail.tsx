import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, CalendarClock, Check, Copy, ExternalLink, WrapText } from "lucide-react";
import { Empty, Loading } from "../../components/primitives";
import { DataTable, type Column } from "../../audience/components/DataTable";
import type { JobPayload, JobRun, ListedRun, RunChild } from "../api";
import { BusyHours, RunsCalendar } from "./CalendarPage";
import { DagGraph, type GraphEdge, type GraphNode } from "../components/DagGraph";
import { Gantt, type GanttRow } from "../components/Gantt";
import { RunBars } from "../components/RunBars";
import { StatusBadge } from "../components/Status";
import { TypeIcon } from "../components/TypeIcon";
import { STATUS_LABEL, duration, nextRun, relative, runSeconds, shortId, when } from "../lib";
import { ganttRows, stepStats, treeOrder } from "../runtree";
import { type DetailTab, useJob, useNow } from "../store";

export type { DetailTab };

const TABS: { id: DetailTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "runs", label: "Runs" },
  { id: "graph", label: "Graph" },
  { id: "gantt", label: "Gantt" },
  { id: "calendar", label: "Calendar" },
  { id: "code", label: "Code" },
];

/** One item: its chain, its runs and what each executed, its schedule, its code. */
export function JobDetail({
  itemId,
  runId,
  tab,
  onTab,
  onRun,
  onOpen,
  onBack,
}: {
  itemId: string;
  runId: string | null;
  tab: DetailTab;
  onTab: (tab: DetailTab) => void;
  onRun: (runId: string) => void;
  onOpen: (id: string, runId?: string) => void;
  onBack: () => void;
}) {
  // Re-read whenever the live answer moved: a running run's steps move on.
  const { page, error } = useJob(itemId);

  if (error) return <p className="banner banner-danger">{error}</p>;
  if (!page) return <Loading what="the item" />;

  const job = page.job;
  const run = page.runs.find((r) => r.runId === runId) ?? page.runs[0] ?? null;
  const next = nextRun(job.schedule?.upcoming);

  return (
    <div className="stack">
      <button type="button" className="back-link link-like" onClick={onBack}>
        <ArrowLeft size={14} aria-hidden="true" /> Back
      </button>

      <section className="card mon-head">
        <div className="mon-head-title">
          <TypeIcon type={job.type} size={18} />
          <h2>{job.name}</h2>
          <span className="chip chip-neutral">{job.typeLabel}</span>
          {job.latest && <StatusBadge status={job.latest.status} />}
          <span className="muted-note">{job.workspace}</span>
          <span className="mon-head-spacer" />
          {job.fabricUrl ? (
            <a className="btn btn-sm" href={job.fabricUrl} target="_blank" rel="noreferrer">
              <ExternalLink size={13} aria-hidden="true" /> Open in Fabric
            </a>
          ) : job.fabricDemo ? (
            <span className="muted-note" title="The demo tenant's items don't exist in Fabric">
              Demo item — not in Fabric
            </span>
          ) : null}
        </div>
        <div className="mon-head-grid">
          <Field label="Schedule">
            {job.schedule ? (
              <>
                <CalendarClock size={14} aria-hidden="true" /> {job.schedule.text}
                {!job.schedule.enabled && <span className="chip chip-amber">off</span>}
              </>
            ) : job.parents.length ? (
              "Run by its orchestrator"
            ) : (
              "None"
            )}
          </Field>
          <Field label="Latest run">
            {job.latest ? (
              <>
                <button type="button" className="link-like mon-head-big" onClick={() => { onRun(job.latest!.runId); onTab("graph"); }}>
                  {when(job.latest.startedAt)}
                </button>
                <StatusBadge status={job.latest.status} compact />
              </>
            ) : (
              "—"
            )}
          </Field>
          <Field label="Next run">
            {next ? (
              <>
                <span className="mon-head-big">{when(next)}</span> <span className="muted-note">{relative(next)}</span>
              </>
            ) : (
              "—"
            )}
          </Field>
          <Field label="Last 5 runs">
            <LastFive runs={page.runs.slice(0, 5)} onRun={(id) => { onRun(id); onTab("graph"); }} />
          </Field>
          <Field label="Run by">
            {job.parents.length
              ? job.parents.map((p, i) => (
                  <span key={p.id}>
                    {i > 0 && ", "}
                    <button type="button" className="link-like" onClick={() => onOpen(p.id)}>
                      {p.name}
                    </button>
                  </span>
                ))
              : "Nothing (top level)"}
          </Field>
        </div>
      </section>

      <nav className="tabs" aria-label="Item">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={tab === t.id ? "tab active" : "tab"}
            onClick={() => onTab(t.id)}
          >
            {t.label}
            {t.id === "runs" && <span className="mon-tab-count">{page.runs.length}</span>}
          </button>
        ))}
      </nav>

      {tab === "overview" && <Overview page={page} onOpen={onOpen} onRun={(id) => { onRun(id); onTab("graph"); }} />}
      {tab === "runs" && <RunsTab page={page} selected={run?.runId ?? null} onRun={(id) => { onRun(id); onTab("graph"); }} onOpen={onOpen} />}
      {tab === "graph" && <GraphTab page={page} run={run} onRun={onRun} onOpen={onOpen} />}
      {tab === "gantt" && <GanttTab page={page} run={run} onRun={onRun} onOpen={onOpen} />}
      {tab === "calendar" && <ItemCalendar page={page} onOpen={onOpen} />}
      {tab === "code" && <CodeTab page={page} />}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mon-field">
      <span className="mon-field-label">{label}</span>
      <span className="mon-field-value">{children}</span>
    </div>
  );
}

/* --------------------------------------------------------------- overview */

function LastFive({ runs, onRun }: { runs: JobRun[]; onRun: (runId: string) => void }) {
  const now = useNow(5_000);
  if (!runs.length) return <span className="muted-note">No runs</span>;
  // Finished runs set the scale; a run still going is clipped at the top.
  const max = Math.max(1, ...runs.map((r) => r.durationSec ?? 0));
  return (
    <span className="mon-last5">
      {[...runs].reverse().map((r) => {
        const sec = runSeconds(r, now);
        return (
          <button
            key={r.runId}
            type="button"
            className="mon-last5-run"
            onClick={() => onRun(r.runId)}
            title={`${when(r.startedAt)} · ${STATUS_LABEL[r.status]} · ${duration(sec)}`}
          >
            <span className="mon-last5-track">
              <span className={`mon-last5-bar mon-${r.status}`} style={{ height: `${Math.min(100, Math.max(14, ((sec ?? 0) / max) * 100))}%` }} />
            </span>
            <span className="mon-last5-sec tnum">{duration(sec)}</span>
          </button>
        );
      })}
    </span>
  );
}

function Overview({
  page,
  onOpen,
  onRun,
}: {
  page: JobPayload;
  onOpen: (id: string) => void;
  onRun: (runId: string) => void;
}) {
  const { chain, job } = page;
  const now = useNow(5_000);
  const nodes: GraphNode[] = chain.nodes.map((n) => ({
    id: n.id,
    title: n.name,
    subtitle: n.typeLabel,
    status: n.latest,
    focus: n.focus,
    ghost: n.latest == null,
    note: n.latest == null ? "no runs of its own" : n.focus ? "this item" : "latest run",
  }));
  const edges: GraphEdge[] = chain.edges.map((e) => ({
    from: e.from,
    to: e.to,
    label: e.via === "Pipeline activity" ? undefined : e.via,
    dashed: e.via === "%run",
  }));
  const latest = page.runs[0] ?? null;
  const links = (refs: { id: string; name: string }[]) =>
    refs.map((p, i) => (
      <span key={p.id}>
        {i > 0 && ", "}
        <button type="button" className="link-like" onClick={() => onOpen(p.id)}>
          {p.name}
        </button>
      </span>
    ));
  const rows: [string, React.ReactNode][] = [
    ["Name", <strong key="n">{job.name}</strong>],
    ["Type", job.typeLabel],
    ["Workspace", job.workspace],
    ["Item ID", <span key="i" className="mono">{job.id}</span>],
    [
      "Schedule",
      job.schedule ? (
        <span key="s">
          {job.schedule.text}
          {!job.schedule.enabled && <span className="chip chip-amber"> off</span>}
        </span>
      ) : (
        <span key="s" className="muted-note">None</span>
      ),
    ],
    ["Run by", job.parents.length ? links(job.parents) : <span className="muted-note">Nothing (top level)</span>],
    [
      "Runs",
      job.children.length ? (
        <span className="mon-deps-cell">
          {job.children.map((c) => (
            <span key={c.id}>
              <button type="button" className="link-like" onClick={() => onOpen(c.id)}>
                {c.name}
              </button>{" "}
              <span className="muted-note">{c.via}</span>
            </span>
          ))}
        </span>
      ) : (
        <span className="muted-note">Nothing</span>
      ),
    ],
    [
      "Last run",
      latest ? (
        <span className="mon-inline">
          <StatusBadge status={latest.status} />
          <button type="button" className="link-like" onClick={() => onRun(latest.runId)}>
            {when(latest.startedAt)}
          </button>
          <span className="muted-note">
            {latest.trigger} · {latest.status === "running" ? `running ${duration(runSeconds(latest, now))}` : duration(latest.durationSec)}
          </span>
        </span>
      ) : (
        <span className="muted-note">No runs in the last {page.windowDays} days</span>
      ),
    ],
    ...(latest?.error ? [["Last error", <span key="e" className="mon-error-cell">{latest.error}</span>] as [string, React.ReactNode]] : []),
    [
      `Last ${page.windowDays} days`,
      `${job.stats.runs} runs · ${job.stats.failed} failed · success ${job.stats.successRate ?? "—"}%`,
    ],
    ["Usual duration", `${duration(job.stats.avgSec)} (longest ${duration(job.stats.maxSec)})`],
    ...(job.reads.length ? [["Reads", <TableList key="r" tables={job.reads} />] as [string, React.ReactNode]] : []),
    ...(job.writes.length ? [["Writes", <TableList key="w" tables={job.writes} />] as [string, React.ReactNode]] : []),
  ];
  return (
    <>
      <div className="mon-grid-2 mon-overview">
        <section className="card">
          <table className="mon-details">
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k}>
                  <th>{k}</th>
                  <td>{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="card mon-pad">
          <div className="mon-section-head">
            <h3>Last 5 runs</h3>
          </div>
          <table className="mon-steps">
            <thead>
              <tr>
                <th>Started</th>
                <th>Status</th>
                <th className="num">Duration</th>
                <th>Triggered by</th>
              </tr>
            </thead>
            <tbody>
              {page.runs.slice(0, 5).map((r) => (
                <tr key={r.runId} className="mon-click" onClick={() => onRun(r.runId)}>
                  <td className="mon-nowrap">{when(r.startedAt)}</td>
                  <td>
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="num tnum">{r.status === "running" ? `${duration(runSeconds(r, now))}…` : duration(r.durationSec)}</td>
                  <td>{r.parent ? r.parent.name : r.trigger}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!page.runs.length && <p className="muted-note">No runs.</p>}
          <div className="mon-section-head" style={{ marginTop: 16 }}>
            <h3>Duration of the last {Math.min(page.runs.length, 60)} runs</h3>
          </div>
          <RunBars
            bars={page.runs
              .slice(0, 60)
              .reverse()
              .map((r) => ({ runId: r.runId, status: r.status, startedAt: r.startedAt, durationSec: r.durationSec }))}
            height={80}
            width={7}
            onPick={onRun}
          />
        </section>
      </div>
      {chain.nodes.length > 1 && (
        <section className="card mon-pad">
          <div className="mon-section-head">
            <h3>Execution chain</h3>
            <p className="muted-note">What runs this item and what it runs. Colour is each item's latest run; click one to open it.</p>
          </div>
          <DagGraph nodes={nodes} edges={edges} selected={job.id} onSelect={(id) => id !== job.id && onOpen(id)} minHeight={160} />
        </section>
      )}
    </>
  );
}

function TableList({ tables }: { tables: string[] }) {
  return (
    <span className="mon-deps-cell">
      {tables.slice(0, 8).map((t) => (
        <span key={t} className="mono">
          {t}
        </span>
      ))}
      {tables.length > 8 && <span className="muted-note">and {tables.length - 8} more</span>}
    </span>
  );
}

/* ------------------------------------------------------------------- runs */

function RunsTab({
  page,
  selected,
  onRun,
  onOpen,
}: {
  page: JobPayload;
  selected: string | null;
  onRun: (runId: string) => void;
  onOpen: (id: string, runId?: string) => void;
}) {
  const columns: Column<JobRun>[] = [
    { key: "run", label: "Run ID", width: "100px", value: (r) => r.runId, render: (r) => <span className="mono" title={r.runId}>{shortId(r.runId)}</span> },
    { key: "start", label: "Start date", width: "170px", value: (r) => r.startedAt ?? "", render: (r) => <span className="mon-nowrap">{when(r.startedAt)}</span> },
    { key: "state", label: "State", width: "140px", value: (r) => r.status, render: (r) => <StatusBadge status={r.status} /> },
    {
      key: "trigger",
      label: "Run type",
      width: "190px",
      value: (r) => r.trigger,
      render: (r) =>
        r.parent ? (
          <span>
            Run by{" "}
            <button
              type="button"
              className="link-like"
              onClick={(e) => {
                e.stopPropagation();
                onOpen(r.parent!.id, r.parent!.runId ?? undefined);
              }}
            >
              {r.parent.name}
            </button>
          </span>
        ) : (
          r.trigger
        ),
    },
    { key: "duration", label: "Duration", width: "110px", value: (r) => r.durationSec ?? -1, render: (r) => duration(r.durationSec) },
    {
      key: "children",
      label: "Children",
      width: "150px",
      value: (r) => r.children.length,
      render: (r) => {
        const failed = r.children.filter((c) => c.status === "failed").length;
        return r.children.length ? (
          <span>
            {r.children.length}
            {failed > 0 && <span className="mon-failed-note"> · {failed} failed</span>}
          </span>
        ) : (
          <span className="muted-note">—</span>
        );
      },
    },
    {
      key: "error",
      label: "Error",
      sortable: false,
      render: (r) => (r.error ? <span className="mon-error-cell" title={r.error}>{r.error}</span> : null),
    },
  ];
  const bars = page.runs
    .slice()
    .reverse()
    .map((r) => ({ runId: r.runId, status: r.status, startedAt: r.startedAt, durationSec: r.durationSec }));
  return (
    <>
      <section className="card mon-pad">
        <RunBars bars={bars} height={90} width={Math.max(4, Math.min(12, Math.floor(900 / Math.max(bars.length, 1)) - 2))} selected={selected} onPick={onRun} />
      </section>
      <DataTable
        columns={columns}
        rows={page.runs}
        rowKey={(r) => r.runId}
        onRowClick={(r) => onRun(r.runId)}
        initialSort="start"
        initialDir="desc"
        exportName={`${page.job.name}-runs`}
        expand={(r) => <RunChildrenList run={r} onOpen={onOpen} />}
        canExpand={(r) => r.children.length > 0 || Boolean(r.error)}
      />
    </>
  );
}

function RunChildrenList({ run, onOpen }: { run: JobRun; onOpen: (id: string, runId?: string) => void }) {
  return (
    <div className="mon-expand">
      {run.error && <pre className="mon-error-box">{run.error}</pre>}
      {treeOrder(run.children).map((c) => (
        <div key={c.key} className="mon-child-row" style={{ paddingLeft: c.depth * 18 }}>
          <StatusBadge status={c.status} compact />
          <strong>{c.name}</strong>
          <span className="muted-note">{c.activityType}</span>
          {c.item && (
            <button type="button" className="link-like" onClick={() => onOpen(c.item!.id, c.runId ?? undefined)}>
              {c.item.name}
            </button>
          )}
          <span className="mon-child-time">{c.timing === "in-session" ? "in session" : duration(c.durationSec)}</span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- run picker */

function RunPicker({ page, run, onRun }: { page: JobPayload; run: JobRun; onRun: (runId: string) => void }) {
  return (
    <select className="select mon-run-picker" value={run.runId} onChange={(e) => onRun(e.target.value)} aria-label="Run">
      {page.runs.map((r) => (
        <option key={r.runId} value={r.runId}>
          {when(r.startedAt)} · {STATUS_LABEL[r.status]} · {duration(runSeconds(r))} · {r.trigger}
        </option>
      ))}
    </select>
  );
}

function RunStrip({ page, run, onRun }: { page: JobPayload; run: JobRun; onRun: (runId: string) => void }) {
  const now = useNow(1000);
  return (
    <div className="mon-run-strip">
      <span className="mon-crumb">
        <span className="mon-field-label">Item</span> {page.job.name}
      </span>
      <span className="mon-crumb-sep">/</span>
      <span className="mon-crumb">
        <span className="mon-field-label">Run</span>
        <StatusBadge status={run.status} compact />
        <RunPicker page={page} run={run} onRun={onRun} />
      </span>
      <span className="muted-note">
        {run.trigger} · {when(run.startedAt)} → {run.endedAt ? when(run.endedAt) : "running"} ·{" "}
        {duration(runSeconds(run, now))}
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ graph */

export function GraphTab({
  page,
  run,
  onRun,
  onOpen,
}: {
  page: JobPayload;
  run: JobRun | null;
  onRun: (runId: string) => void;
  onOpen: (id: string, runId?: string) => void;
}) {
  const [task, setTask] = useState<string | null>(null);
  useEffect(() => setTask(null), [run?.runId]);
  const { nodes, edges } = useMemo(() => runGraph(page, run), [page, run]);
  if (!run) return <Empty title="No runs">This item has no runs in the last {page.windowDays} days.</Empty>;
  const selected = run.children.find((c) => c.key === task) ?? null;
  return (
    <>
      <RunStrip page={page} run={run} onRun={onRun} />
      <div className="mon-split">
        <section className="card mon-graph-card">
          {run.children.length ? (
            <DagGraph nodes={nodes} edges={edges} selected={task} onSelect={setTask} minHeight={360} />
          ) : (
            <p className="muted-note mon-pad">
              {page.job.type === "Notebook"
                ? "This notebook calls no other notebooks."
                : "Fabric reported no activities for this run."}
            </p>
          )}
          <p className="muted-note mon-graph-legend">
            Solid arrows: runs after. Dashed: runs inside (a child pipeline's activities, a notebook's in-session
            notebooks). Faded boxes ran inside their caller's Spark session, so Fabric gives them no time of their own.
          </p>
        </section>
        <aside className="card mon-side">
          {selected ? (
            <TaskPanel task={selected} onOpen={onOpen} />
          ) : (
            <RunPanel run={run} />
          )}
        </aside>
      </div>
    </>
  );
}

function runGraph(page: JobPayload, run: JobRun | null): { nodes: GraphNode[]; edges: GraphEdge[] } {
  if (!run) return { nodes: [], edges: [] };
  const keys = new Set(run.children.map((c) => c.key));
  const nodes: GraphNode[] = run.children.map((c) => ({
    id: c.key,
    title: c.name,
    subtitle: c.item && c.item.name !== c.name ? `${c.activityType} · ${c.item.name}` : c.activityType,
    status: c.status,
    ghost: c.timing === "in-session",
    note: c.timing === "in-session" ? "in session" : duration(c.durationSec),
  }));
  const edges: GraphEdge[] = [];
  for (const c of run.children) {
    for (const d of c.dependsOn) if (keys.has(d)) edges.push({ from: d, to: c.key });
    if (c.parentKey && keys.has(c.parentKey)) edges.push({ from: c.parentKey, to: c.key, dashed: true });
  }
  void page;
  return { nodes, edges };
}

function RunPanel({ run }: { run: JobRun }) {
  const now = useNow(1000);
  const failed = run.children.filter((c) => c.status === "failed");
  const measured = run.children.filter((c) => c.timing === "measured" && c.durationSec != null);
  const longest = [...measured].sort((a, b) => (b.durationSec ?? 0) - (a.durationSec ?? 0)).slice(0, 5);
  return (
    <div className="stack-sm">
      <div className="mon-side-head">
        <h3>Run {shortId(run.runId)}</h3>
        <StatusBadge status={run.status} />
      </div>
      <dl className="mon-dl">
        <dt>Run type</dt>
        <dd>{run.trigger}</dd>
        <dt>Start</dt>
        <dd>{when(run.startedAt)}</dd>
        <dt>End</dt>
        <dd>{run.endedAt ? when(run.endedAt) : run.status === "running" ? "Still running" : "—"}</dd>
        <dt>{run.status === "running" ? "So far" : "Duration"}</dt>
        <dd>{duration(runSeconds(run, now))}</dd>
      </dl>
      {run.error && <pre className="mon-error-box">{run.error}</pre>}
      {failed.length > 0 && (
        <p className="mon-failed-note">
          {failed.length} task{failed.length === 1 ? "" : "s"} failed: {failed.map((f) => f.name).join(", ")}
        </p>
      )}
      {longest.length > 0 && (
        <>
          <h4 className="mon-side-sub">Longest tasks</h4>
          <ul className="mon-longest">
            {longest.map((c) => (
              <li key={c.key}>
                <span>{c.name}</span>
                <span className="tnum">{duration(c.durationSec)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="muted-note">Click a task in the graph for its details.</p>
    </div>
  );
}

function TaskPanel({ task, onOpen }: { task: RunChild; onOpen: (id: string, runId?: string) => void }) {
  return (
    <div className="stack-sm">
      <div className="mon-side-head">
        <h3>{task.name}</h3>
        <StatusBadge status={task.status} />
      </div>
      <dl className="mon-dl">
        <dt>Activity</dt>
        <dd>{task.activityType}</dd>
        {task.item && (
          <>
            <dt>Item</dt>
            <dd>
              <button type="button" className="link-like" onClick={() => onOpen(task.item!.id, task.runId ?? undefined)}>
                {task.item.name}
              </button>{" "}
              <span className="muted-note">{task.item.typeLabel}</span>
            </dd>
          </>
        )}
        <dt>Start</dt>
        <dd>{task.timing === "in-session" ? "—" : when(task.startedAt)}</dd>
        <dt>End</dt>
        <dd>{task.timing === "in-session" ? "—" : when(task.endedAt)}</dd>
        <dt>Duration</dt>
        <dd>{task.timing === "in-session" ? "—" : duration(task.durationSec)}</dd>
        {task.runId && (
          <>
            <dt>Its run</dt>
            <dd className="mono">{shortId(task.runId)}</dd>
          </>
        )}
      </dl>
      {task.timing === "in-session" && (
        <p className="banner banner-info">
          Ran inside its caller's Spark session ({task.activityType}). Fabric records one run for the caller and
          none for this notebook, so its own start, end and outcome aren't available — the caller's run covers it.
        </p>
      )}
      {task.error && <pre className="mon-error-box">{task.error}</pre>}
      {task.item && task.runId && (
        <button type="button" className="btn btn-sm" onClick={() => onOpen(task.item!.id, task.runId!)}>
          Open this run of {task.item.name}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ gantt */

function GanttTab({
  page,
  run,
  onRun,
  onOpen,
}: {
  page: JobPayload;
  run: JobRun | null;
  onRun: (runId: string) => void;
  onOpen: (id: string, runId?: string) => void;
}) {
  const now = useNow(5_000);
  const stats = useMemo(() => stepStats(page), [page]);
  const [mode, setMode] = useState<"history" | "one">("history");
  const [count, setCount] = useState(20);
  if (!run) return <Empty title="No runs">This item has no runs in the last {page.windowDays} days.</Empty>;
  const byKey = new Map(run.children.map((c) => [c.key, c]));
  const rows = ganttRows(page, run, stats);
  return (
    <>
      <div className="mon-toolbar">
        <div className="segmented" role="group" aria-label="Gantt">
          <button type="button" className={mode === "history" ? "active" : ""} onClick={() => setMode("history")}>
            Run history
          </button>
          <button type="button" className={mode === "one" ? "active" : ""} onClick={() => setMode("one")}>
            One run
          </button>
        </div>
        {mode === "history" && (
          <div className="segmented" role="group" aria-label="Runs">
            {[10, 20, 50].map((n) => (
              <button key={n} type="button" className={count === n ? "active" : ""} onClick={() => setCount(n)}>
                Last {n}
              </button>
            ))}
          </div>
        )}
      </div>
      {mode === "history" ? (
        <section className="card mon-pad">
          <HistoryGrid
            page={page}
            runs={page.runs.slice(0, count).reverse()}
            now={now}
            selected={run.runId}
            onPick={(runId) => {
              onRun(runId);
              setMode("one");
            }}
          />
          <p className="muted-note mon-graph-legend">
            One column per run, oldest left; one row per step. Bar height is how long the step took against its own longest
            run, colour is how it ended. Hatched: it ran inside its caller's session, so it has no times of its own. Click a
            column to see that run as a Gantt.
          </p>
        </section>
      ) : (
        <>
          <RunStrip page={page} run={run} onRun={onRun} />
          <section className="card mon-pad">
            <Gantt
              rows={rows}
              now={now}
              onSelect={(key) => {
                const c = byKey.get(key);
                if (c?.item) onOpen(c.item.id, c.runId ?? undefined);
              }}
            />
            <p className="muted-note mon-graph-legend">
              Each bar is a task's own start and end, as Fabric reported it. Hatched rows ran inside their caller's
              session and have no times of their own. Click a task with an item to open it.
            </p>
          </section>
        </>
      )}
    </>
  );
}

/** Steps × runs: which step got slower, which one fails, run after run. */
function HistoryGrid({
  page,
  runs,
  now,
  selected,
  onPick,
}: {
  page: JobPayload;
  runs: JobRun[];
  now: number;
  selected: string;
  onPick: (runId: string) => void;
}) {
  // Rows: the steps of the newest run in tree order, then steps only older runs had.
  const steps = useMemo(() => {
    const out: RunChild[] = [];
    const seen = new Set<string>();
    for (const r of [...runs].reverse()) {
      for (const c of treeOrder(r.children)) {
        if (seen.has(c.key)) continue;
        seen.add(c.key);
        out.push(c);
      }
    }
    return out;
  }, [runs]);
  const cell = useMemo(() => {
    const m = new Map<string, RunChild>();
    for (const r of runs) for (const c of r.children) m.set(`${r.runId}|${c.key}`, c);
    return m;
  }, [runs]);
  const maxOf = useMemo(() => {
    const m = new Map<string, number>();
    // Finished steps set the scale; a long-running one is clipped, not flattening the rest.
    for (const r of runs)
      for (const c of r.children) if (c.durationSec != null) m.set(c.key, Math.max(m.get(c.key) ?? 1, c.durationSec));
    return m;
  }, [runs]);
  const runMax = Math.max(1, ...runs.map((r) => r.durationSec ?? 0));
  const cols = `minmax(220px, 300px) 70px repeat(${runs.length}, minmax(14px, 1fr))`;

  const bar = (sec: number | null, max: number, status: string, untimed: boolean, title: string, runId: string) => (
    <button
      key={runId}
      type="button"
      className={`mon-hist-cell${runId === selected ? " selected" : ""}`}
      title={title}
      onClick={() => onPick(runId)}
    >
      {untimed ? (
        <span className="mon-hist-bar mon-untimed" style={{ height: "40%" }} />
      ) : (
        <span className={`mon-hist-bar mon-${status}`} style={{ height: `${Math.min(100, Math.max(10, ((sec ?? 0) / max) * 100))}%` }} />
      )}
    </button>
  );

  return (
    <div className="mon-hist" role="table">
      <div className="mon-hist-row mon-hist-head" style={{ gridTemplateColumns: cols }}>
        <span className="muted-note">Step</span>
        <span className="muted-note num">Usual</span>
        {runs.map((r) => (
          <span key={r.runId} className="mon-hist-date" title={when(r.startedAt)}>
            {new Date(r.startedAt ?? 0).toLocaleDateString(undefined, { day: "numeric", month: "numeric" })}
          </span>
        ))}
      </div>
      <div className="mon-hist-row mon-hist-total" style={{ gridTemplateColumns: cols }}>
        <span className="mon-hist-label">
          <strong>{page.job.name}</strong>
          <span className="muted-note">whole run</span>
        </span>
        <span className="num tnum muted-note">{duration(page.job.stats.avgSec)}</span>
        {runs.map((r) =>
          bar(runSeconds(r, now), runMax, r.status, false, `${when(r.startedAt)} · ${STATUS_LABEL[r.status]} · ${duration(runSeconds(r, now))}`, r.runId),
        )}
      </div>
      {steps.map((s) => {
        const ok = runs
          .map((r) => cell.get(`${r.runId}|${s.key}`))
          .filter((c): c is RunChild => !!c && c.status === "success" && c.durationSec != null);
        const avg = ok.length ? ok.reduce((a, c) => a + c.durationSec!, 0) / ok.length : null;
        return (
          <div key={s.key} className="mon-hist-row" style={{ gridTemplateColumns: cols }}>
            <span className="mon-hist-label" style={{ paddingLeft: 4 + s.depth * 14 }}>
              <strong>{s.name}</strong>
              <span className="muted-note">{s.item && s.item.name !== s.name ? `${s.activityType} · ${s.item.name}` : s.activityType}</span>
            </span>
            <span className="num tnum muted-note">{s.timing === "in-session" ? "in session" : duration(avg)}</span>
            {runs.map((r) => {
              const c = cell.get(`${r.runId}|${s.key}`);
              if (!c) return <span key={r.runId} className="mon-hist-cell mon-hist-none" title={`${when(r.startedAt)}: not in this run`} />;
              const sec = runSeconds(c, now);
              return bar(
                sec,
                maxOf.get(s.key) ?? 1,
                c.status,
                c.timing === "in-session",
                `${s.name} · ${when(r.startedAt)} · ${STATUS_LABEL[c.status]}${c.timing === "in-session" ? " · in session" : ` · ${duration(sec)}`}`,
                r.runId,
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

/* --------------------------------------------------------------- calendar */

/** The item's month, its picked day hour by hour, and when in the week it runs. */
function ItemCalendar({ page, onOpen }: { page: JobPayload; onOpen: (id: string, runId?: string) => void }) {
  const now = useNow(60_000);
  const { job } = page;
  const runs = useMemo<ListedRun[]>(
    () =>
      page.runs.map((r) => ({
        ...r,
        item: { id: job.id, name: job.name, type: job.type, typeLabel: job.typeLabel },
        workspace: job.workspace,
        workspaceId: job.workspaceId,
      })),
    [page, job],
  );
  return (
    <>
      <RunsCalendar jobs={[job]} runs={runs} onOpen={onOpen} />
      <BusyHours
        runs={runs}
        from={now - page.windowDays * 86_400_000}
        to={now}
        title={`When ${job.name} runs, last ${page.windowDays} days`}
      />
    </>
  );
}

/* ------------------------------------------------------------------- code */

function CodeTab({ page }: { page: JobPayload }) {
  const [file, setFile] = useState(0);
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);
  if (!page.code.length) {
    return (
      <Empty title="No code">
        {page.job.type === "SemanticModel"
          ? "A semantic model's definition is on its Data Catalog page."
          : "The scan could not read this item's definition."}
      </Empty>
    );
  }
  const current = page.code[Math.min(file, page.code.length - 1)];
  const lines = current.text.split("\n");
  return (
    <section className="card mon-code">
      <div className="mon-code-bar">
        <div className="mon-code-files">
          {page.code.map((c, i) => (
            <button key={c.partPath} type="button" className={i === file ? "chip chip-green" : "chip chip-neutral"} onClick={() => setFile(i)}>
              {c.partPath}
            </button>
          ))}
        </div>
        <span className="muted-note">{lines.length} lines · as read by the last scan</span>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => {
            navigator.clipboard?.writeText(current.text).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />} {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className={wrap ? "btn btn-sm active" : "btn btn-sm"} onClick={() => setWrap((w) => !w)} aria-pressed={wrap}>
          <WrapText size={13} /> Wrap
        </button>
      </div>
      <pre className={`code-block mon-code-block${wrap ? " wrap" : ""}`}>
        {lines.map((line, i) => (
          <span key={i} className="code-line">
            <span className="code-num" aria-hidden="true">
              {i + 1}
            </span>
            {line || " "}
            {"\n"}
          </span>
        ))}
      </pre>
    </section>
  );
}
