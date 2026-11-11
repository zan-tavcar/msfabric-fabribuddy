import type { RunStatus } from "../api";
import { STATUS_LABEL, duration, when } from "../lib";

export interface GanttRow {
  key: string;
  label: string;
  sub?: string;
  depth: number;
  status: RunStatus;
  start: string | null;
  end: string | null;
  /** Ran inside its caller's session: Fabric gives it no times of its own. */
  untimed?: boolean;
  /** For an untimed row: the span it sits within (its caller's). */
  within?: { start: string | null; end: string | null };
  /** Shown in the tooltip, e.g. "usual 7m 45s". */
  hint?: string;
  /** Not run yet: a schedule's next run, drawn as an outline its usual length. */
  planned?: boolean;
  /** Several runs on one lane (a week or a month, one row per item). */
  bars?: { key: string; status: RunStatus; start: string | null; end: string | null; planned?: boolean }[];
}

const TICKS = 6;

/**
 * Start and end of every task on one time axis, green succeeded, red failed.
 * A running task's bar grows to `now` (striped); `range` fixes the window
 * (a day, the last 6 hours) instead of fitting the rows.
 */
export function Gantt({
  rows,
  selected,
  onSelect,
  onBar,
  labelWidth = 300,
  now,
  range,
}: {
  rows: GanttRow[];
  selected?: string | null;
  onSelect?: (key: string) => void;
  /** A bar on a lane row was clicked. */
  onBar?: (rowKey: string, barKey: string) => void;
  labelWidth?: number;
  now?: number;
  range?: [number, number];
}) {
  const at = (iso: string | null) => (iso ? new Date(iso).getTime() : null);
  const endOf = (r: GanttRow) => at(r.end) ?? (r.status === "running" || r.status === "queued" ? now ?? null : null);
  const times = rows
    .flatMap((r) => [at(r.start), endOf(r), ...(r.bars ?? []).flatMap((b) => [at(b.start), at(b.end)])])
    .filter((t): t is number => t != null);
  if (!range && !times.length) return <p className="muted-note">No timings reported.</p>;
  const t0 = range ? range[0] : Math.min(...times);
  const t1 = range ? range[1] : Math.max(...times, t0 + 1000);
  const span = Math.max(t1 - t0, 1000);
  const pct = (t: number) => ((t - t0) / span) * 100;
  const clamp = (v: number) => Math.min(100, Math.max(0, v));
  const nowPct = now != null && now >= t0 && now <= t1 ? pct(now) : null;
  const tickLabel = (t: number) => {
    const d = new Date(t);
    const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: span < 600_000 ? "2-digit" : undefined });
    return span > 26 * 3_600_000 ? `${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${time}` : time;
  };

  return (
    <div className="mon-gantt">
      <div className="mon-gantt-head" style={{ gridTemplateColumns: `${labelWidth}px 1fr` }}>
        <span className="muted-note">Task</span>
        <div className="mon-gantt-axis">
          {Array.from({ length: TICKS + 1 }, (_, i) => (
            <span key={i} style={{ left: `${(i / TICKS) * 100}%` }}>
              {tickLabel(t0 + (span * i) / TICKS)}
            </span>
          ))}
        </div>
      </div>
      {rows.length === 0 && <p className="muted-note mon-pad">Nothing ran in this window.</p>}
      {rows.map((r) => {
        const s = r.untimed ? at(r.within?.start ?? null) ?? t0 : at(r.start);
        const e = r.untimed ? at(r.within?.end ?? null) ?? now ?? t1 : endOf(r);
        const running = !r.untimed && !r.planned && r.end == null && (r.status === "running" || r.status === "queued");
        const from = s == null ? null : clamp(pct(s));
        const to = e == null ? from : clamp(pct(e));
        const width = from == null || to == null ? 0 : Math.max(0.4, to - from);
        const secs = s != null && e != null ? (e - s) / 1000 : null;
        const tip = r.planned
          ? `${r.label}: scheduled ${when(r.start)}${r.hint ? ` · ${r.hint}` : ""}`
          : r.untimed
          ? `${r.label}: ran inside its caller's session — Fabric reports no separate start or end`
          : `${r.label}: ${STATUS_LABEL[r.status]} · ${when(r.start)} → ${running ? "now" : when(r.end)} · ${duration(secs)}${r.hint ? ` · ${r.hint}` : ""}`;
        const text = r.planned ? "planned" : r.untimed ? "in session" : running ? `${duration(secs)}…` : duration(secs);
        return (
          <button
            type="button"
            key={r.key}
            className={`mon-gantt-row${selected === r.key ? " selected" : ""}`}
            style={{ gridTemplateColumns: `${labelWidth}px 1fr` }}
            onClick={() => onSelect?.(r.key)}
            title={tip}
          >
            <span className="mon-gantt-label" style={{ paddingLeft: 8 + r.depth * 16 }}>
              <span className="mon-gantt-name">{r.label}</span>
              {r.sub && <span className="mon-gantt-sub">{r.sub}</span>}
            </span>
            <span className="mon-gantt-track">
              {Array.from({ length: TICKS - 1 }, (_, i) => (
                <span key={i} className="mon-gantt-grid" style={{ left: `${((i + 1) / TICKS) * 100}%` }} />
              ))}
              {nowPct != null && <span className="mon-gantt-now" style={{ left: `${nowPct}%` }} />}
              {r.bars?.map((b) => {
                const bs = at(b.start);
                if (bs == null) return null;
                const live = b.end == null && (b.status === "running" || b.status === "queued") && !b.planned;
                const be = at(b.end) ?? (live ? now ?? t1 : bs + 60_000);
                const l = clamp(pct(bs));
                const w = Math.max(0.25, clamp(pct(be)) - l);
                const secs = (be - bs) / 1000;
                return (
                  <span
                    key={b.key}
                    role="button"
                    tabIndex={-1}
                    className={`mon-gantt-bar mon-lane-bar mon-${b.planned ? "planned" : b.status}${live ? " live" : ""}`}
                    style={{ left: `${l}%`, width: `${w}%` }}
                    title={`${r.label}: ${b.planned ? "planned" : STATUS_LABEL[b.status]} · ${when(b.start)} · ${duration(secs)}`}
                    onClick={(e) => {
                      if (!onBar) return;
                      e.stopPropagation();
                      onBar(r.key, b.key);
                    }}
                  />
                );
              })}
              {!r.bars && from != null && (
                <span
                  className={`mon-gantt-bar mon-${r.planned ? "planned" : r.untimed ? "untimed" : r.status}${running ? " live" : ""}`}
                  style={{ left: `${from}%`, width: `${width}%` }}
                >
                  {/* Too narrow to read: the tooltip carries the duration. */}
                  {width < 5 ? null : text}
                </span>
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}
