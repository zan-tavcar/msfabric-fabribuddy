import { useMemo } from "react";
import { CalendarClock } from "lucide-react";
import type { JobSummary, JobsPayload, RunsPayload } from "../api";
import { DataTable, type Column } from "../../audience/components/DataTable";
import { MonitorFilters } from "../components/MonitorFilters";
import { inRange, keepStatus, rangeLabel, rangeMs, rangeStats, type RangeStats } from "../filters";
import { RunBars } from "../components/RunBars";
import { StatusBadge } from "../components/Status";
import { TypeIcon } from "../components/TypeIcon";
import { useMonitor, useNow } from "../store";
import { duration, nextRun, runSeconds, when } from "../lib";


/**
 * Every runnable item — pipelines, notebooks, dataflows, semantic models: its
 * schedule, latest and next run, its numbers over the chosen range, and its
 * last runs as bars.
 */
export function Jobs({
  all,
  data,
  runs,
  running,
  onOpen,
}: {
  all: JobsPayload;
  data: JobsPayload;
  runs: RunsPayload | null;
  /** Items with a run in progress (from the live answer). */
  running: Set<string>;
  onOpen: (id: string, runId?: string) => void;
}) {
  const { filters } = useMonitor();
  const now = useNow(15_000);
  const t0 = now - rangeMs(filters.range);

  const stats = useMemo(
    () => rangeStats((runs?.runs ?? []).filter((r) => inRange(r, t0, now))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [runs, filters.range, Math.floor(now / 60_000)],
  );

  const rows = useMemo(() => {
    const out = data.jobs.filter((j) => keepStatus(running.has(j.id) ? "running" : j.latest?.status, filters));
    const latest = (j: JobSummary) => (j.latest?.startedAt ? new Date(j.latest.startedAt).getTime() : 0);
    return [...out].sort((a, b) => latest(b) - latest(a));
  }, [data, filters, running]);

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Items</h2>
        <p className="page-head-sub">
          Every runnable item: schedule, latest and next run, success and usual duration over the {rangeLabel(filters.range)}, and
          its last runs (height is duration). Click one for its runs, graph, Gantt, calendar and code.
        </p>
      </header>

      <MonitorFilters jobs={all} count={`${rows.length} item${rows.length === 1 ? "" : "s"}`} statusLabel="Last run status" />

      <JobTable rows={rows} onOpen={onOpen} now={now} stats={stats} running={running} />
    </div>
  );
}

function JobTable({
  rows,
  onOpen,
  now,
  stats,
  running,
}: {
  rows: JobSummary[];
  onOpen: (id: string, runId?: string) => void;
  now: number;
  stats: Map<string, RangeStats>;
  running: Set<string>;
}) {
  const columns: Column<JobSummary>[] = [
    {
      key: "name",
      label: "Item",
      value: (j) => j.name.toLowerCase(),
      render: (j) => (
        <span className="mon-name-cell">
          <TypeIcon type={j.type} />
          <span className="mon-deps-cell">
            <span className="strong link-like">{j.name}</span>
            <span className="muted-note">
              {j.typeLabel} · {j.workspace}
            </span>
          </span>
        </span>
      ),
    },
    {
      key: "status",
      label: "Last run status",
      width: "140px",
      value: (j) => j.latest?.status ?? "",
      render: (j) =>
        running.has(j.id) ? (
          <StatusBadge status="running" />
        ) : j.latest ? (
          <StatusBadge status={j.latest.status} />
        ) : (
          <span className="muted-note">{j.parents.some((p) => p.type === "Notebook") ? "In caller's session" : "Never run"}</span>
        ),
    },
    {
      key: "schedule",
      label: "Schedule",
      width: "190px",
      value: (j) => j.schedule?.text ?? "",
      render: (j) =>
        j.schedule ? (
          <span className={j.schedule.enabled ? "" : "muted-note"}>
            <CalendarClock size={12} aria-hidden="true" /> {j.schedule.text}
            {!j.schedule.enabled && " (off)"}
          </span>
        ) : j.parents.length ? (
          <span className="muted-note">Run by {j.parents.map((p) => p.name).join(", ")}</span>
        ) : (
          <span className="muted-note">None</span>
        ),
    },
    {
      key: "latest",
      label: "Latest run",
      width: "160px",
      value: (j) => j.latest?.startedAt ?? "",
      render: (j) => (
        <span className="mon-deps-cell">
          <span className="mon-nowrap">{when(j.latest?.startedAt)}</span>
          {j.latest && (
            <span className="muted-note">
              {j.latest.status === "running" ? `running ${duration(runSeconds(j.latest, now))}` : duration(j.latest.durationSec)}
            </span>
          )}
        </span>
      ),
    },
    {
      key: "next",
      label: "Next run",
      width: "160px",
      value: (j) => nextRun(j.schedule?.upcoming, now) ?? "",
      render: (j) => <span className="mon-nowrap">{when(nextRun(j.schedule?.upcoming, now))}</span>,
    },
    {
      key: "runs",
      label: "Runs",
      width: "90px",
      value: (j) => stats.get(j.id)?.runs ?? 0,
      render: (j) => {
        const st = stats.get(j.id);
        return (
          <span className="mon-deps-cell tnum">
            <span>{st?.runs ?? 0}</span>
            {st?.failed ? <span className="mon-failed-note">{st.failed} failed</span> : null}
          </span>
        );
      },
    },
    { key: "avg", label: "Usual", width: "90px", value: (j) => stats.get(j.id)?.avgSec ?? -1, render: (j) => <span className="mon-nowrap">{duration(stats.get(j.id)?.avgSec)}</span> },
    {
      key: "rate",
      label: "Success",
      width: "80px",
      value: (j) => stats.get(j.id)?.successRate ?? -1,
      render: (j) => (stats.get(j.id)?.successRate != null ? `${stats.get(j.id)!.successRate}%` : "—"),
    },
    {
      key: "bars",
      label: "Run history",
      width: "190px",
      sortable: false,
      render: (j) => <RunBars bars={j.bars.slice(-16)} height={26} width={6} onPick={(runId) => onOpen(j.id, runId)} />,
    },
  ];
  return <DataTable columns={columns} rows={rows} rowKey={(j) => j.id} onRowClick={(j) => onOpen(j.id)} exportName="job-monitor-items" />;
}
