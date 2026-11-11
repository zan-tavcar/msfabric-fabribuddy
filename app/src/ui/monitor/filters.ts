/**
 * The filters every monitor page shares: workspace, item type, item, status,
 * role in a chain, a keyword, and a time range. One set, kept in the store, so moving
 * from Runs to the Gantt to the Calendar keeps what was picked.
 */
import type { JobSummary, ListedRun, RunStatus } from "./api";
import { STATUS_LABEL } from "./lib";

export type Range = "6h" | "24h" | "7d" | "30d";

export const RANGES: { id: Range; label: string; next: string; ms: number }[] = [
  { id: "6h", label: "Last 6 h", next: "Next 6 h", ms: 6 * 3_600_000 },
  { id: "24h", label: "Last 24 h", next: "Next 24 h", ms: 24 * 3_600_000 },
  { id: "7d", label: "Last 7 days", next: "Next 7 days", ms: 7 * 86_400_000 },
  { id: "30d", label: "Last 30 days", next: "Next 30 days", ms: 30 * 86_400_000 },
];

export const rangeMs = (r: Range) => RANGES.find((x) => x.id === r)!.ms;
export const rangeLabel = (r: Range) => RANGES.find((x) => x.id === r)!.label.replace(/^Last /, "last ");

export interface Filters {
  workspaces: string[];
  types: string[];
  /** Item names. */
  items: string[];
  statuses: string[];
  roles: string[];
  q: string;
  range: Range;
}

export const NO_FILTERS: Filters = { workspaces: [], types: [], items: [], statuses: [], roles: [], q: "", range: "7d" };

export const ROLE = {
  orchestrator: "Runs other items",
  child: "Run by other items",
  standalone: "Standalone",
} as const;

export function roleOf(j: JobSummary): string[] {
  const out: string[] = [];
  if (j.children.length) out.push(ROLE.orchestrator);
  if (j.parents.length) out.push(ROLE.child);
  if (!out.length) out.push(ROLE.standalone);
  return out;
}

export const statusName = (s: RunStatus | null | undefined) => (s ? STATUS_LABEL[s] : "Never run");

/** The items the item-level filters keep (status is per page: last run, or each run). */
export function keepJob(j: JobSummary, f: Filters): boolean {
  if (f.workspaces.length && !f.workspaces.includes(j.workspace)) return false;
  if (f.types.length && !f.types.includes(j.typeLabel)) return false;
  if (f.items.length && !f.items.includes(j.name)) return false;
  if (f.roles.length && !roleOf(j).some((r) => f.roles.includes(r))) return false;
  const needle = f.q.trim().toLowerCase();
  if (needle && !`${j.name} ${j.workspace} ${j.typeLabel}`.toLowerCase().includes(needle)) return false;
  return true;
}

export const keepStatus = (s: RunStatus | null | undefined, f: Filters) =>
  !f.statuses.length || f.statuses.includes(statusName(s));

/** A run overlaps [t0, now]: started in it, or still going into it. */
export function inRange(r: Pick<ListedRun, "startedAt" | "endedAt">, t0: number, now: number): boolean {
  if (!r.startedAt) return false;
  const s = new Date(r.startedAt).getTime();
  const e = r.endedAt ? new Date(r.endedAt).getTime() : now;
  return s <= now && e >= t0;
}

export function isFiltered(f: Filters): boolean {
  return Boolean(f.workspaces.length || f.types.length || f.items.length || f.statuses.length || f.roles.length || f.q.trim());
}

/** Per-item numbers over the range, from the runs list. */
export interface RangeStats {
  runs: number;
  finished: number;
  failed: number;
  successRate: number | null;
  avgSec: number | null;
}

export function rangeStats(runs: ListedRun[]): Map<string, RangeStats> {
  const acc = new Map<string, { runs: number; finished: number; failed: number; ok: number; secs: number[] }>();
  for (const r of runs) {
    const a = acc.get(r.item.id) ?? { runs: 0, finished: 0, failed: 0, ok: 0, secs: [] };
    a.runs++;
    if (r.status === "success" || r.status === "failed" || r.status === "cancelled") a.finished++;
    if (r.status === "failed") a.failed++;
    if (r.status === "success") {
      a.ok++;
      if (r.durationSec != null) a.secs.push(r.durationSec);
    }
    acc.set(r.item.id, a);
  }
  const out = new Map<string, RangeStats>();
  for (const [id, a] of acc) {
    out.set(id, {
      runs: a.runs,
      finished: a.finished,
      failed: a.failed,
      successRate: a.finished ? Math.round((1000 * a.ok) / a.finished) / 10 : null,
      avgSec: a.secs.length ? a.secs.reduce((x, y) => x + y, 0) / a.secs.length : null,
    });
  }
  return out;
}
