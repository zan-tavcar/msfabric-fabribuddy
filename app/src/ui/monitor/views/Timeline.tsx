import { useMemo, useState } from "react";
import type { JobsPayload, ListedRun, LivePayload, RunsPayload } from "../api";
import { MonitorFilters } from "../components/MonitorFilters";
import { Gantt, type GanttRow } from "../components/Gantt";
import { keepStatus, rangeLabel, rangeMs } from "../filters";
import { duration } from "../lib";
import { useMonitor, useNow } from "../store";

/**
 * Every run on one time axis. Over 6 or 24 hours each run is a row and a run
 * started by another sits under it (pl_master → pl_ingest_d365 → df_fx_rates),
 * so a chain reads top to bottom. Over 7 or 30 days each item is one lane with
 * all its runs on it — where the nightly load crept later, which nights failed.
 * Running bars grow to the "now" line; the schedules' next runs are outlines.
 */
export function Timeline({
  all,
  runs,
  live,
  onOpen,
}: {
  all: JobsPayload;
  runs: RunsPayload;
  live: LivePayload | null;
  onOpen: (id: string, runId?: string) => void;
}) {
  const { filters } = useMonitor();
  const now = useNow(5_000);
  const [planned, setPlanned] = useState(true);
  const span = rangeMs(filters.range);
  const lanes = span > 26 * 3_600_000;
  // A little of what's next, so planned runs and the "now" line show.
  const window_: [number, number] = useMemo(
    () => [now - span, now + span / 6],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [span, Math.floor(now / 30_000)],
  );

  const { rows, shown } = useMemo(() => {
    const [t0, t1] = window_;
    const inWindow = runs.runs.filter((r) => {
      if (!r.startedAt) return false;
      const s = new Date(r.startedAt).getTime();
      const e = r.endedAt ? new Date(r.endedAt).getTime() : now;
      return s < t1 && e > t0;
    });
    const upcoming = planned
      ? (live?.upcoming ?? []).filter((u) => {
          const at = new Date(u.at).getTime();
          return at >= Math.max(t0, now) && at <= t1;
        })
      : [];

    if (lanes) {
      // One lane per item: its runs in the window, and its planned runs.
      const by = new Map<string, { run: ListedRun; list: ListedRun[] }>();
      for (const r of inWindow) {
        if (!keepStatus(r.status, filters)) continue;
        const e = by.get(r.item.id) ?? { run: r, list: [] };
        e.list.push(r);
        by.set(r.item.id, e);
      }
      const out: GanttRow[] = [...by.values()]
        .sort((a, b) => a.run.item.name.localeCompare(b.run.item.name))
        .map(({ run, list }) => {
          const failed = list.filter((r) => r.status === "failed").length;
          return {
            key: run.item.id,
            label: run.item.name,
            sub: `${run.item.typeLabel} · ${list.length} run${list.length === 1 ? "" : "s"}${failed ? ` · ${failed} failed` : ""}`,
            depth: 0,
            status: run.status,
            start: null,
            end: null,
            bars: [
              ...list.map((r) => ({ key: r.runId, status: r.status, start: r.startedAt, end: r.endedAt })),
              ...upcoming
                .filter((u) => u.item.id === run.item.id)
                .map((u) => ({
                  key: `planned:${u.at}`,
                  status: "queued" as const,
                  start: u.at,
                  end: new Date(new Date(u.at).getTime() + (u.expectedSec ?? 300) * 1000).toISOString(),
                  planned: true,
                })),
            ],
          };
        });
      return { rows: out, shown: [...by.values()].reduce((n, e) => n + e.list.length, 0) };
    }

    const ids = new Set(inWindow.map((r) => r.runId));
    const kids = new Map<string, ListedRun[]>();
    const roots: ListedRun[] = [];
    for (const r of inWindow) {
      const parent = r.parent?.runId;
      if (parent && ids.has(parent)) {
        if (!kids.has(parent)) kids.set(parent, []);
        kids.get(parent)!.push(r);
      } else roots.push(r);
    }
    // A chain stays whole when any run in it matches the filters.
    const matches = (r: ListedRun): boolean => keepStatus(r.status, filters) || (kids.get(r.runId) ?? []).some(matches);
    const out: GanttRow[] = [];
    let count = 0;
    const walk = (r: ListedRun, depth: number) => {
      count++;
      out.push({
        key: r.runId,
        label: r.item.name,
        sub: `${r.item.typeLabel} · ${r.trigger}`,
        depth,
        status: r.status,
        start: r.startedAt,
        end: r.endedAt,
      });
      [...(kids.get(r.runId) ?? [])]
        .sort((a, b) => (a.startedAt ?? "").localeCompare(b.startedAt ?? ""))
        .forEach((k) => walk(k, depth + 1));
    };
    roots
      .filter(matches)
      .sort((a, b) => (a.startedAt ?? "").localeCompare(b.startedAt ?? ""))
      .forEach((r) => walk(r, 0));
    for (const u of upcoming) {
      const at = new Date(u.at).getTime();
      out.push({
        key: `planned:${u.item.id}:${u.at}`,
        label: u.item.name,
        sub: `${u.item.typeLabel} · Scheduled`,
        depth: 0,
        status: "queued",
        start: u.at,
        end: new Date(at + (u.expectedSec ?? 300) * 1000).toISOString(),
        planned: true,
        hint: u.expectedSec != null ? `usually ${duration(u.expectedSec)}` : "no history yet",
      });
    }
    return { rows: out, shown: count };
  }, [runs, live, window_, filters, planned, now, lanes]);

  const byId = useMemo(() => new Map(runs.runs.map((r) => [r.runId, r])), [runs]);

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Gantt</h2>
        <p className="page-head-sub">
          {lanes
            ? `The ${rangeLabel(filters.range)}, one lane per item: every run on it, so drift, overlaps and failed nights show.`
            : "Every run on one time axis. A run started by another run sits under it, with its own start and end — follow a chain top to bottom."}
        </p>
      </header>

      <MonitorFilters jobs={all} count={`${shown} run${shown === 1 ? "" : "s"}`}>
        <label className="mon-check">
          <input type="checkbox" checked={planned} onChange={(e) => setPlanned(e.target.checked)} /> Planned runs
        </label>
      </MonitorFilters>

      <div className="card mon-pad">
        <Gantt
          rows={rows}
          now={now}
          range={window_}
          labelWidth={lanes ? 240 : 280}
          onSelect={(key) => {
            if (lanes) return onOpen(key);
            if (key.startsWith("planned:")) return onOpen(key.split(":")[1]);
            const r = byId.get(key);
            if (r) onOpen(r.item.id, r.runId);
          }}
          onBar={(rowKey, barKey) => onOpen(rowKey, barKey.startsWith("planned:") ? undefined : barKey)}
        />
        <div className="mon-cal-legend">
          <span><i className="mon-cal-swatch mon-sw-ok" /> Succeeded</span>
          <span><i className="mon-cal-swatch mon-sw-fail" /> Failed</span>
          <span><i className="mon-cal-swatch mon-sw-run" /> Running</span>
          <span><i className="mon-cal-swatch mon-sw-planned" /> Planned (schedule, usual length)</span>
          <span><i className="mon-cal-swatch mon-sw-now" /> Now</span>
        </div>
      </div>
    </div>
  );
}
