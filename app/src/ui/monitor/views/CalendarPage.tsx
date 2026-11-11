import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { JobSummary, JobsPayload, ListedRun, LivePayload, RunStatus, RunsPayload } from "../api";
import { MonitorFilters } from "../components/MonitorFilters";
import { inRange, keepStatus } from "../filters";
import { DAY_MS, STATUS_LABEL, dayKey, duration, when } from "../lib";
import { useMonitor, useNow } from "../store";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOUR_PX = 44;
/** Monday = 0. */
const weekday = (d: Date) => (d.getDay() + 6) % 7;

interface DayCell {
  ok: number;
  failed: number;
  other: number;
  planned: number;
  seconds: number;
  failedItems: Set<string>;
}

interface Block {
  key: string;
  itemId: string;
  runId?: string;
  name: string;
  type: string;
  status: RunStatus | "planned";
  start: number;
  end: number;
  col: number;
  cols: number;
}

/**
 * The calendar: a month of runs (each day's outcome, how long things ran,
 * what the schedules will start), the picked day hour by hour with every run
 * as a block, and the hours of the week the capacity is busiest.
 */
export function CalendarPage({
  all,
  jobs,
  runs,
  onOpen,
}: {
  all: JobsPayload;
  jobs: JobsPayload;
  runs: RunsPayload;
  live: LivePayload | null;
  onOpen: (id: string, runId?: string) => void;
}) {
  const { filters } = useMonitor();
  const shownRuns = useMemo(() => runs.runs.filter((r) => keepStatus(r.status, filters)), [runs, filters]);
  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Calendar</h2>
        <p className="page-head-sub">
          Every day's runs and failures, what the schedules will start, and the picked day hour by hour.
        </p>
      </header>
      <MonitorFilters jobs={all} count={`${shownRuns.length.toLocaleString()} runs`} ranges={false} />
      <RunsCalendar jobs={jobs.jobs} runs={shownRuns} showPlanned={!filters.statuses.length} onOpen={onOpen} />
    </div>
  );
}

/** A month of runs beside the picked day, hour by hour. */
export function RunsCalendar({
  jobs,
  runs: shownRuns,
  showPlanned = true,
  onOpen,
}: {
  jobs: JobSummary[];
  runs: ListedRun[];
  showPlanned?: boolean;
  onOpen: (id: string, runId?: string) => void;
}) {
  const now = useNow(30_000);
  const [month, setMonth] = useState(() => {
    const d = new Date(now);
    return new Date(d.getFullYear(), d.getMonth(), 1);
  });
  const [day, setDay] = useState(() => dayKey(now));

  // The schedules' starts, each as long as that item usually runs.
  const planned = useMemo(
    () =>
      jobs.flatMap((j) =>
        j.schedule?.enabled
          ? j.schedule.upcoming
              .map((at) => new Date(at).getTime())
              .filter((t) => t > now)
              .map((t) => ({ job: j, at: t, sec: j.stats.avgSec ?? 300 }))
          : [],
      ),
    [jobs, now],
  );

  const cells = useMemo(() => {
    const out = new Map<string, DayCell>();
    const cell = (k: string) => {
      let c = out.get(k);
      if (!c) out.set(k, (c = { ok: 0, failed: 0, other: 0, planned: 0, seconds: 0, failedItems: new Set() }));
      return c;
    };
    for (const r of shownRuns) {
      if (!r.startedAt) continue;
      const c = cell(dayKey(r.startedAt));
      if (r.status === "success") c.ok++;
      else if (r.status === "failed") {
        c.failed++;
        c.failedItems.add(r.item.name);
      } else c.other++;
      c.seconds += r.durationSec ?? 0;
    }
    if (showPlanned) for (const p of planned) cell(dayKey(p.at)).planned++;
    return out;
  }, [shownRuns, planned, showPlanned]);

  // The month grid: whole weeks, Monday first.
  const weeks = useMemo(() => {
    const first = new Date(month);
    const start = new Date(first.getFullYear(), first.getMonth(), 1 - weekday(first));
    const out: Date[][] = [];
    for (let w = 0; w < 6; w++) {
      const week: Date[] = [];
      for (let d = 0; d < 7; d++) week.push(new Date(start.getFullYear(), start.getMonth(), start.getDate() + w * 7 + d));
      if (w > 3 && week[0].getMonth() !== month.getMonth()) break;
      out.push(week);
    }
    return out;
  }, [month]);
  const maxDay = Math.max(1, ...weeks.flat().map((d) => {
    const c = cells.get(dayKey(d.getTime()));
    return c ? c.ok + c.failed + c.other : 0;
  }));

  const today = dayKey(now);
  const monthTotals = weeks
    .flat()
    .filter((d) => d.getMonth() === month.getMonth())
    .reduce(
      (acc, d) => {
        const c = cells.get(dayKey(d.getTime()));
        if (c) {
          acc.runs += c.ok + c.failed + c.other;
          acc.failed += c.failed;
          if (c.failed) acc.badDays++;
          acc.seconds += c.seconds;
        }
        return acc;
      },
      { runs: 0, failed: 0, badDays: 0, seconds: 0 },
    );

  const shiftMonth = (n: number) => setMonth(new Date(month.getFullYear(), month.getMonth() + n, 1));

  return (
    <>
      <div className="mon-calx">
        <section className="card mon-calx-month">
          <div className="mon-calx-head">
            <button type="button" className="btn btn-icon btn-sm" aria-label="Previous month" onClick={() => shiftMonth(-1)}>
              <ChevronLeft size={15} />
            </button>
            <h3>{month.toLocaleDateString(undefined, { month: "long", year: "numeric" })}</h3>
            <button type="button" className="btn btn-icon btn-sm" aria-label="Next month" onClick={() => shiftMonth(1)}>
              <ChevronRight size={15} />
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => {
                const d = new Date(now);
                setMonth(new Date(d.getFullYear(), d.getMonth(), 1));
                setDay(today);
              }}
            >
              Today
            </button>
            <span className="muted-note mon-calx-sum">
              {monthTotals.runs} runs · <span className={monthTotals.failed ? "mon-failed-note" : ""}>{monthTotals.failed} failed</span>{" "}
              on {monthTotals.badDays} day{monthTotals.badDays === 1 ? "" : "s"} · {duration(monthTotals.seconds)} of run time
            </span>
          </div>
          <div className="mon-calx-grid">
            {WEEKDAYS.map((w) => (
              <span key={w} className="mon-calx-wd">
                {w}
              </span>
            ))}
            {weeks.flat().map((d) => {
              const k = dayKey(d.getTime());
              const c = cells.get(k);
              const total = c ? c.ok + c.failed + c.other : 0;
              const outside = d.getMonth() !== month.getMonth();
              const future = k > today;
              return (
                <button
                  key={k}
                  type="button"
                  className={[
                    "mon-calx-day",
                    outside ? "outside" : "",
                    k === today ? "today" : "",
                    k === day ? "picked" : "",
                    c?.failed ? "bad" : "",
                  ].join(" ")}
                  onClick={() => setDay(k)}
                  aria-pressed={k === day}
                  title={
                    c
                      ? `${d.toDateString()}: ${total} runs, ${c.failed} failed${c.failed ? ` (${[...c.failedItems].join(", ")})` : ""}${
                          c.planned ? `, ${c.planned} planned` : ""
                        }`
                      : d.toDateString()
                  }
                >
                  <span className="mon-calx-num">{d.getDate()}</span>
                  {total > 0 && (
                    <>
                      <span className="mon-calx-bar" style={{ width: `${Math.max(8, (total / maxDay) * 100)}%` }}>
                        {c!.ok > 0 && <i className="ok" style={{ flexGrow: c!.ok }} />}
                        {c!.other > 0 && <i className="other" style={{ flexGrow: c!.other }} />}
                        {c!.failed > 0 && <i className="fail" style={{ flexGrow: c!.failed }} />}
                      </span>
                      <span className="mon-calx-meta tnum">
                        {total} run{total === 1 ? "" : "s"}
                        {c!.failed > 0 && <strong className="mon-failed-note"> · {c!.failed} failed</strong>}
                      </span>
                      {c!.failed > 0 && <span className="mon-calx-items">{[...c!.failedItems].slice(0, 2).join(", ")}</span>}
                    </>
                  )}
                  {(future || k === today) && c?.planned ? (
                    <span className="mon-calx-planned tnum">{c.planned} planned</span>
                  ) : null}
                </button>
              );
            })}
          </div>
          <div className="mon-cal-legend">
            <span><i className="mon-cal-swatch mon-sw-ok" /> Succeeded</span>
            <span><i className="mon-cal-swatch mon-sw-fail" /> Failed</span>
            <span><i className="mon-cal-swatch mon-sw-other" /> Running, cancelled</span>
            <span><i className="mon-cal-swatch mon-sw-planned" /> Planned by a schedule</span>
          </div>
        </section>

        <DayView day={day} runs={shownRuns} planned={showPlanned ? planned : []} now={now} onOpen={onOpen} onDay={setDay} />
      </div>
    </>
  );
}

/** The picked day, hour by hour: each run a block, side by side when they overlap. */
function DayView({
  day,
  runs,
  planned,
  now,
  onOpen,
  onDay,
}: {
  day: string;
  runs: ListedRun[];
  planned: { job: { id: string; name: string; type: string }; at: number; sec: number }[];
  now: number;
  onOpen: (id: string, runId?: string) => void;
  onDay: (day: string) => void;
}) {
  const t0 = new Date(`${day}T00:00:00`).getTime();
  const t1 = t0 + DAY_MS;
  const scroller = useRef<HTMLDivElement>(null);

  const blocks = useMemo(() => {
    const list: Omit<Block, "col" | "cols">[] = [];
    // A chain is one block: its top run, with how many runs it started.
    const ids = new Set(runs.map((r) => r.runId));
    const started = new Map<string, number>();
    const rootOf = (r: ListedRun): string | null => (r.parent?.runId && ids.has(r.parent.runId) ? r.parent.runId : null);
    const byRun = new Map(runs.map((r) => [r.runId, r]));
    for (const r of runs) {
      let p = rootOf(r);
      let guard = 0;
      while (p && guard++ < 10) {
        const up = byRun.get(p);
        const next = up ? rootOf(up) : null;
        if (!next) break;
        p = next;
      }
      if (p) started.set(p, (started.get(p) ?? 0) + 1);
    }
    for (const r of runs) {
      if (!r.startedAt || rootOf(r)) continue;
      const s = new Date(r.startedAt).getTime();
      const e = r.endedAt ? new Date(r.endedAt).getTime() : r.status === "running" ? now : s + (r.durationSec ?? 60) * 1000;
      if (e < t0 || s >= t1) continue;
      const kids = started.get(r.runId);
      list.push({ key: `${r.item.id}:${r.runId}`, itemId: r.item.id, runId: r.runId, name: kids ? `${r.item.name} +${kids}` : r.item.name, type: r.item.typeLabel, status: r.status, start: Math.max(s, t0), end: Math.min(e, t1) });
    }
    for (const p of planned) {
      if (p.at < t0 || p.at >= t1) continue;
      list.push({ key: `planned:${p.job.id}:${p.at}`, itemId: p.job.id, name: p.job.name, type: "Scheduled", status: "planned", start: p.at, end: Math.min(p.at + p.sec * 1000, t1) });
    }
    list.sort((a, b) => a.start - b.start || b.end - a.end);
    // Overlapping runs share the width: greedy columns per cluster.
    const out: Block[] = [];
    let cluster: Block[] = [];
    let clusterEnd = -Infinity;
    const minEnd = (b: { start: number; end: number }) => Math.max(b.end, b.start + 20 * 60_000); // a short run still takes room
    const flush = () => {
      const cols = Math.max(1, ...cluster.map((b) => b.col + 1));
      cluster.forEach((b) => (b.cols = cols));
      out.push(...cluster);
      cluster = [];
    };
    for (const b of list) {
      if (b.start >= clusterEnd && cluster.length) flush();
      const used = new Set(cluster.filter((x) => minEnd(x) > b.start).map((x) => x.col));
      let col = 0;
      while (used.has(col)) col++;
      cluster.push({ ...b, col, cols: 1 });
      clusterEnd = Math.max(clusterEnd, minEnd(b));
    }
    if (cluster.length) flush();
    return out;
  }, [runs, planned, t0, t1, now]);

  // Open on the first run of the day (or now).
  useEffect(() => {
    const first = blocks[0]?.start;
    const at = dayKey(now) === day ? now - 2 * 3_600_000 : first ? first - 1_800_000 : t0 + 6 * 3_600_000;
    scroller.current?.scrollTo({ top: Math.max(0, ((at - t0) / 3_600_000) * HOUR_PX) });
  }, [day]); // eslint-disable-line react-hooks/exhaustive-deps

  const date = new Date(`${day}T12:00:00`);
  const failed = blocks.filter((b) => b.status === "failed").length;
  const ran = blocks.filter((b) => b.status !== "planned").length;
  const shift = (n: number) => onDay(dayKey(date.getTime() + n * DAY_MS));
  const nowTop = now >= t0 && now < t1 ? ((now - t0) / 3_600_000) * HOUR_PX : null;

  return (
    <section className="card mon-calx-dayview">
      <div className="mon-calx-head">
        <button type="button" className="btn btn-icon btn-sm" aria-label="Previous day" onClick={() => shift(-1)}>
          <ChevronLeft size={15} />
        </button>
        <h3>{date.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}</h3>
        <button type="button" className="btn btn-icon btn-sm" aria-label="Next day" onClick={() => shift(1)}>
          <ChevronRight size={15} />
        </button>
      </div>
      <p className="muted-note mon-calx-daysum">
        {ran} run{ran === 1 ? "" : "s"}
        {failed > 0 && <span className="mon-failed-note"> · {failed} failed</span>}
        {blocks.length > ran && ` · ${blocks.length - ran} planned`}
      </p>
      <div className="mon-calx-hours" ref={scroller}>
        <div className="mon-calx-canvas" style={{ height: 24 * HOUR_PX }}>
          {Array.from({ length: 24 }, (_, h) => (
            <div key={h} className="mon-calx-hour" style={{ top: h * HOUR_PX }}>
              <span>{String(h).padStart(2, "0")}:00</span>
            </div>
          ))}
          {nowTop != null && <div className="mon-calx-now" style={{ top: nowTop }} />}
          {blocks.map((b) => {
            const top = ((b.start - t0) / 3_600_000) * HOUR_PX;
            const height = Math.max(18, ((b.end - b.start) / 3_600_000) * HOUR_PX);
            const secs = (b.end - b.start) / 1000;
            return (
              <button
                key={b.key}
                type="button"
                className={`mon-calx-block mon-${b.status}`}
                style={{ top, height, left: `calc(52px + (100% - 56px) * ${b.col / b.cols})`, width: `calc((100% - 56px) / ${b.cols} - 3px)` }}
                title={`${b.name} · ${b.status === "planned" ? "planned" : STATUS_LABEL[b.status]} · ${when(new Date(b.start).toISOString()).slice(11, 16)} · ${duration(secs)}`}
                onClick={() => onOpen(b.itemId, b.runId)}
              >
                <strong>{b.name}</strong>
                {height >= 30 && (
                  <span>
                    {when(new Date(b.start).toISOString()).slice(11, 16)} · {b.status === "planned" ? "planned" : duration(secs)}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}

/**
 * Minutes of runs per hour of the week, over the range: where the capacity is
 * crowded (and where a new schedule fits). A red corner: a run failed then.
 */
export function BusyHours({
  runs,
  from: t0,
  to: now,
  title,
}: {
  runs: ListedRun[];
  from: number;
  to: number;
  title: string;
}) {
  const grid = useMemo(() => {
    const minutes = Array.from({ length: 7 }, () => new Array<number>(24).fill(0));
    const starts = Array.from({ length: 7 }, () => new Array<number>(24).fill(0));
    const fails = Array.from({ length: 7 }, () => new Array<number>(24).fill(0));
    for (const r of runs) {
      if (!inRange(r, t0, now) || !r.startedAt) continue;
      const s = Math.max(new Date(r.startedAt).getTime(), t0);
      const e = r.endedAt ? new Date(r.endedAt).getTime() : now;
      const d0 = new Date(r.startedAt);
      starts[weekday(d0)][d0.getHours()]++;
      if (r.status === "failed") fails[weekday(d0)][d0.getHours()]++;
      // Spread its run time over the hours it covered.
      for (let t = s; t < e; ) {
        const d = new Date(t);
        const next = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours() + 1).getTime();
        const upTo = Math.min(next, e);
        minutes[weekday(d)][d.getHours()] += (upTo - t) / 60_000;
        t = upTo;
      }
    }
    return { minutes, starts, fails };
  }, [runs, t0, now]);
  const max = Math.max(1, ...grid.minutes.flat());
  const busiest = grid.minutes
    .flatMap((row, w) => row.map((m, h) => ({ w, h, m })))
    .sort((a, b) => b.m - a.m)
    .filter((x) => x.m > 0)
    .slice(0, 3);

  return (
    <section className="card mon-pad">
      <div className="mon-section-head">
        <h3>{title}</h3>
        <p className="muted-note">
          Run time per hour of the week: the darker, the longer it ran then. A red corner: a run failed in that hour.
          {busiest.length > 0 &&
            ` Busiest: ${busiest.map((b) => `${WEEKDAYS[b.w]} ${String(b.h).padStart(2, "0")}:00 (${Math.round(b.m)} min)`).join(", ")}.`}
        </p>
      </div>
      <div className="mon-busy">
        <span />
        {Array.from({ length: 24 }, (_, h) => (
          <span key={h} className="mon-busy-h">
            {h % 3 === 0 ? String(h).padStart(2, "0") : ""}
          </span>
        ))}
        {WEEKDAYS.map((w, wi) => (
          <div key={w} className="mon-busy-row">
            <span className="mon-busy-wd">{w}</span>
            {grid.minutes[wi].map((m, h) => (
              <span
                key={h}
                className={`mon-busy-cell${grid.fails[wi][h] ? " failed" : ""}`}
                style={{ background: m ? `rgba(64, 136, 112, ${0.12 + 0.88 * (m / max)})` : undefined }}
                title={`${w} ${String(h).padStart(2, "0")}:00 – ${String(h + 1).padStart(2, "0")}:00 · ${Math.round(m)} min of runs · ${grid.starts[wi][h]} started${
                  grid.fails[wi][h] ? ` · ${grid.fails[wi][h]} failed` : ""
                }`}
              />
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}
