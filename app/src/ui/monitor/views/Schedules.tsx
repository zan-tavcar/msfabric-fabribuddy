import { useMemo } from "react";
import { CalendarClock } from "lucide-react";
import type { JobSummary, JobsPayload } from "../api";
import { DataTable, type Column } from "../../audience/components/DataTable";
import { RunBars } from "../components/RunBars";
import { StatusBadge } from "../components/Status";
import { TypeIcon } from "../components/TypeIcon";
import { duration, nextRun, relative, when } from "../lib";
import { MonitorFilters } from "../components/MonitorFilters";
import { RANGES, keepStatus, rangeMs } from "../filters";
import { useMonitor, useNow } from "../store";


/**
 * Every schedule: what it starts, when next, how long that usually takes — and
 * the next 24 hours laid out, so overlapping runs on one capacity show.
 */
export function Schedules({
  all,
  jobs,
  onOpen,
}: {
  all: JobsPayload;
  jobs: JobsPayload;
  onOpen: (id: string, runId?: string) => void;
}) {
  const { filters } = useMonitor();
  const now = useNow(30_000);
  const kept = useMemo(() => jobs.jobs.filter((j) => keepStatus(j.latest?.status, filters)), [jobs, filters]);
  const scheduled = useMemo(() => kept.filter((j) => j.schedule), [kept]);
  const unscheduled = kept.filter((j) => !j.schedule && !j.parents.length);
  const doubled = kept.filter((j) => j.schedule?.enabled && j.parents.length);
  const horizon = rangeMs(filters.range);
  const label = RANGES.find((x) => x.id === filters.range)!.next.replace(/^Next /, "next ");
  const t0 = now;
  const t1 = now + horizon;
  const days = horizon > 26 * 3_600_000;
  const pct = (t: number) => ((t - t0) / (t1 - t0)) * 100;

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
    { key: "ws", label: "Workspace", value: (j) => j.workspace, render: (j) => j.workspace },
    {
      key: "schedule",
      label: "Schedule",
      value: (j) => j.schedule?.text ?? "",
      render: (j) => (
        <span>
          {j.schedule?.text}
          {!j.schedule?.enabled && <span className="chip chip-amber"> off</span>}
        </span>
      ),
    },
    {
      key: "next",
      label: "Next run",
      width: "210px",
      value: (j) => nextRun(j.schedule?.upcoming, now) ?? "~",
      render: (j) => {
        const n = nextRun(j.schedule?.upcoming, now);
        return n ? (
          <span className="mon-nowrap">
            {when(n).slice(0, 16)} <span className="muted-note">{relative(n, now)}</span>
          </span>
        ) : (
          "—"
        );
      },
    },
    { key: "usual", label: "Usually takes", width: "120px", value: (j) => j.stats.avgSec ?? -1, render: (j) => duration(j.stats.avgSec) },
    {
      key: "last",
      label: "Last run",
      width: "150px",
      value: (j) => j.latest?.status ?? "",
      render: (j) => (j.latest ? <StatusBadge status={j.latest.status} /> : <span className="muted-note">Never</span>),
    },
    {
      key: "last5",
      label: "Last 5 runs",
      width: "120px",
      sortable: false,
      render: (j) => <RunBars bars={j.bars.slice(-5)} height={24} width={8} onPick={(runId) => onOpen(j.id, runId)} />,
    },
    {
      key: "rate",
      label: "Success",
      width: "90px",
      value: (j) => j.stats.successRate ?? -1,
      render: (j) => (j.stats.successRate != null ? `${j.stats.successRate}%` : "—"),
    },
  ];

  const ticks = Array.from({ length: 7 }, (_, i) => t0 + (i * (t1 - t0)) / 6);

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Schedules</h2>
        <p className="page-head-sub">
          {scheduled.filter((j) => j.schedule?.enabled).length} active schedules. Items run by an orchestrator start with
          it; they are on the Dependencies page.
        </p>
      </header>

      <MonitorFilters
        jobs={all}
        count={`${scheduled.length} schedule${scheduled.length === 1 ? "" : "s"}`}
        statusLabel="Last run status"
        ranges="next"
      />

      {doubled.length > 0 && (
        <p className="banner banner-warn">
          <strong>Started twice:</strong> {doubled.map((j) => j.name).join(", ")}{" "}
          {doubled.length === 1 ? "has" : "have"} a schedule of {doubled.length === 1 ? "its" : "their"} own and{" "}
          {doubled.length === 1 ? "is" : "are"} also run by{" "}
          {[...new Set(doubled.flatMap((j) => j.parents.map((p) => p.name)))].join(", ")}. Unless that is meant, one of the two
          is extra capacity — or runs before its data is ready.
        </p>
      )}

      <section className="card mon-pad">
        <div className="mon-section-head">
          <h3>
            <CalendarClock size={15} aria-hidden="true" /> The {label}
          </h3>
          <p className="muted-note">Each block is a scheduled start, as long as that item usually runs. Overlaps share capacity.</p>
        </div>
        <div className="mon-plan">
          <div className="mon-plan-axis">
            <span />
            <div>
              {ticks.map((t) => (
                <span key={t} style={{ left: `${pct(t)}%` }}>
                  {days
                    ? new Date(t).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })
                    : new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
                </span>
              ))}
            </div>
          </div>
          {scheduled
            .filter((j) => j.schedule?.enabled)
            .map((j) => {
              const starts = (j.schedule?.upcoming ?? []).map((u) => new Date(u).getTime()).filter((t) => t > t0 && t < t1);
              return (
                <button key={j.id} type="button" className="mon-plan-row" onClick={() => onOpen(j.id)}>
                  <span className="mon-plan-label">
                    <TypeIcon type={j.type} /> {j.name}
                  </span>
                  <span className="mon-plan-track">
                    {starts.map((t) => (
                      <span
                        key={t}
                        className="mon-plan-block"
                        style={{ left: `${pct(t)}%`, width: `${Math.max(0.5, pct(t + (j.stats.avgSec ?? 120) * 1000) - pct(t))}%` }}
                        title={`${j.name}: ${when(new Date(t).toISOString())} · usually ${duration(j.stats.avgSec)}`}
                      />
                    ))}
                    {!starts.length && <span className="muted-note mon-plan-none">nothing in the {label}</span>}
                  </span>
                </button>
              );
            })}
        </div>
      </section>

      <DataTable
        columns={columns}
        rows={scheduled}
        rowKey={(j) => j.id}
        onRowClick={(j) => onOpen(j.id)}
        initialSort="next"
        initialDir="asc"
        exportName="job-monitor-schedules"
      />

      {unscheduled.length > 0 && (
        <section className="card mon-pad">
          <div className="mon-section-head">
            <h3>Not scheduled and not run by anything</h3>
            <p className="muted-note">Run by hand, by the API, or not at all.</p>
          </div>
          <ul className="mon-feed">
            {unscheduled.map((j) => (
              <li key={j.id}>
                <button type="button" className="mon-feed-row" onClick={() => onOpen(j.id)}>
                  <TypeIcon type={j.type} />
                  <span className="mon-feed-main">
                    <strong>{j.name}</strong>
                    <span className="muted-note">
                      {j.workspace} · {j.latest ? `last run ${when(j.latest.startedAt)} (${j.latest.trigger})` : "never run"}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
