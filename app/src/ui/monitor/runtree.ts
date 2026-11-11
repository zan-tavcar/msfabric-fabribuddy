/** One run's children as a tree: display order, Gantt rows, per-step statistics. */
import type { JobPayload, JobRun, RunChild, RunStatus } from "./api";
import type { GanttRow } from "./components/Gantt";
import { duration } from "./lib";

/** Children in display order: each parent followed by its own, by start time. */
export function treeOrder(children: RunChild[]): RunChild[] {
  const byParent = new Map<string | null, RunChild[]>();
  for (const c of children) {
    const k = c.parentKey;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k)!.push(c);
  }
  const keys = new Set(children.map((c) => c.key));
  const out: RunChild[] = [];
  const walk = (parent: string | null) => {
    for (const c of [...(byParent.get(parent) ?? [])].sort((a, b) => (a.startedAt ?? "~").localeCompare(b.startedAt ?? "~"))) {
      out.push(c);
      walk(c.key);
    }
  };
  walk(null);
  // Orphans (a parent key we don't have): at the end, not lost.
  for (const c of children) if (c.parentKey && !keys.has(c.parentKey) && !out.includes(c)) out.push(c);
  return out;
}

/** The run itself, then every child with its own start and end. */
export function ganttRows(page: JobPayload, run: JobRun, stats?: Map<string, StepStats>): GanttRow[] {
  const byKey = new Map(run.children.map((c) => [c.key, c]));
  const spanOf = (c: RunChild): { start: string | null; end: string | null } => {
    let p = c.parentKey ? byKey.get(c.parentKey) : undefined;
    while (p && p.timing === "in-session") p = p.parentKey ? byKey.get(p.parentKey) : undefined;
    return p ? { start: p.startedAt, end: p.endedAt } : { start: run.startedAt, end: run.endedAt };
  };
  return [
    {
      key: "__run",
      label: page.job.name,
      sub: `${page.job.typeLabel} · ${run.trigger}`,
      depth: 0,
      status: run.status,
      start: run.startedAt,
      end: run.endedAt,
    },
    ...treeOrder(run.children).map((c) => {
      const s = stats?.get(c.key);
      return {
        key: c.key,
        label: c.name,
        sub: c.item && c.item.name !== c.name ? `${c.activityType} · ${c.item.name}` : c.activityType,
        depth: c.depth + 1,
        status: c.status,
        start: c.startedAt,
        end: c.endedAt,
        untimed: c.timing === "in-session",
        within: c.timing === "in-session" ? spanOf(c) : undefined,
        hint: s?.avgSec != null ? `usually ${duration(s.avgSec)} (${s.runs} runs)` : undefined,
      };
    }),
  ];
}

export interface StepStats {
  key: string;
  runs: number;
  failed: number;
  avgSec: number | null;
  maxSec: number | null;
  minSec: number | null;
  /** Durations oldest first, for a sparkline. */
  history: { sec: number | null; status: RunStatus }[];
}

/** Per step (child key) across every run of an item: how long it usually takes. */
export function stepStats(page: JobPayload): Map<string, StepStats> {
  const out = new Map<string, StepStats>();
  for (const run of [...page.runs].reverse()) {
    for (const c of run.children) {
      const s =
        out.get(c.key) ??
        ({ key: c.key, runs: 0, failed: 0, avgSec: null, maxSec: null, minSec: null, history: [] } as StepStats);
      s.runs += 1;
      if (c.status === "failed") s.failed += 1;
      s.history.push({ sec: c.durationSec, status: c.status });
      out.set(c.key, s);
    }
  }
  for (const s of out.values()) {
    const ok = s.history.filter((h) => h.status === "success" && h.sec != null).map((h) => h.sec!);
    if (ok.length) {
      s.avgSec = ok.reduce((a, b) => a + b, 0) / ok.length;
      s.maxSec = Math.max(...ok);
      s.minSec = Math.min(...ok);
    }
  }
  return out;
}
