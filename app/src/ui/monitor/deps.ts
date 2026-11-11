/**
 * What depends on what, two ways:
 *  - runs: an item starts another (pipeline activity, notebook.run, runMultiple,
 *    %run) or a schedule starts it;
 *  - data: an item reads a table another item writes.
 * And the checks a data engineer runs by eye: is every data dependency ordered
 * by something, did a reader start before its writer finished or after it
 * failed, is anything started twice.
 */
import type { DataDependency, JobSummary, JobsPayload, ListedRun } from "./api";

export function descendantsOf(id: string, byId: Map<string, JobSummary>): Set<string> {
  const out = new Set<string>();
  const walk = (x: string) => {
    for (const c of byId.get(x)?.children ?? []) {
      if (out.has(c.id)) continue;
      out.add(c.id);
      walk(c.id);
    }
  };
  walk(id);
  return out;
}

/** The orchestrator that runs both (or one runs the other), if any. */
export function orderedBy(a: string, b: string, byId: Map<string, JobSummary>): JobSummary | null {
  let best: JobSummary | null = null;
  let bestSize = Infinity;
  for (const j of byId.values()) {
    if (!j.children.length) continue;
    const d = descendantsOf(j.id, byId);
    const hasA = j.id === a || d.has(a);
    const hasB = j.id === b || d.has(b);
    // The smallest chain that holds both is the one that orders them.
    if (hasA && hasB && d.size < bestSize) {
      best = j;
      bestSize = d.size;
    }
  }
  return best;
}

export type CheckKind = "unordered" | "overlap" | "after-failure" | "twice";

export interface Check {
  kind: CheckKind;
  /** The item the finding is about (the reader, or the item started twice). */
  item: JobSummary;
  /** The upstream item, for a data finding. */
  upstream?: JobSummary;
  tables?: string[];
  count?: number;
  example?: { run: ListedRun; upstreamRun: ListedRun };
  text: string;
}

const t = (iso: string | null) => (iso ? new Date(iso).getTime() : NaN);

/**
 * For each run of B in the range: the latest run of A that started before it.
 * Still running when B started: B read half-written tables. Failed: B read
 * yesterday's (or broken) data.
 */
function runEvidence(
  a: string,
  b: string,
  runsByItem: Map<string, ListedRun[]>,
  t0: number,
): { overlap: ListedRun[][]; afterFailure: ListedRun[][] } {
  const overlap: ListedRun[][] = [];
  const afterFailure: ListedRun[][] = [];
  const upstream = runsByItem.get(a) ?? []; // newest first
  for (const rb of runsByItem.get(b) ?? []) {
    const start = t(rb.startedAt);
    if (!(start >= t0)) continue;
    const ra = upstream.find((r) => t(r.startedAt) < start);
    if (!ra || start - t(ra.startedAt) > 26 * 3_600_000) continue;
    // A child of A started by A itself is ordered by A: not a finding.
    if (rb.parent?.runId && rb.parent.runId === ra.runId) continue;
    const end = ra.endedAt ? t(ra.endedAt) : Infinity;
    if (end > start) overlap.push([rb, ra]);
    else if (ra.status === "failed") afterFailure.push([rb, ra]);
  }
  return { overlap, afterFailure };
}

export function dependencyChecks(jobs: JobsPayload, runs: ListedRun[], t0: number): Check[] {
  const byId = new Map(jobs.jobs.map((j) => [j.id, j]));
  const runsByItem = new Map<string, ListedRun[]>();
  for (const r of runs) {
    if (!runsByItem.has(r.item.id)) runsByItem.set(r.item.id, []);
    runsByItem.get(r.item.id)!.push(r);
  }
  const out: Check[] = [];
  for (const d of jobs.data ?? []) {
    const a = byId.get(d.from);
    const b = byId.get(d.to);
    if (!a || !b) continue;
    const by = orderedBy(d.from, d.to, byId);
    if (!by && (b.schedule?.enabled || b.latest)) {
      out.push({
        kind: "unordered",
        item: b,
        upstream: a,
        tables: d.tables,
        text: `${b.name} reads what ${a.name} writes, but nothing runs them in order: they start on their own${
          b.schedule ? ` (${b.name}: ${b.schedule.text})` : ""
        }.`,
      });
    }
    const ev = runEvidence(d.from, d.to, runsByItem, t0);
    if (ev.overlap.length) {
      const [run, upstreamRun] = ev.overlap[0];
      out.push({
        kind: "overlap",
        item: b,
        upstream: a,
        tables: d.tables,
        count: ev.overlap.length,
        example: { run, upstreamRun },
        text: `${b.name} started while ${a.name} was still writing its input ${ev.overlap.length}×.`,
      });
    }
    if (ev.afterFailure.length) {
      const [run, upstreamRun] = ev.afterFailure[0];
      out.push({
        kind: "after-failure",
        item: b,
        upstream: a,
        tables: d.tables,
        count: ev.afterFailure.length,
        example: { run, upstreamRun },
        text: `${b.name} ran after ${a.name} failed ${ev.afterFailure.length}×: it read stale or partial data.`,
      });
    }
  }
  for (const j of jobs.jobs) {
    if (j.schedule?.enabled && j.parents.length) {
      out.push({
        kind: "twice",
        item: j,
        text: `${j.name} has its own schedule (${j.schedule.text}) and is also run by ${j.parents
          .map((p) => p.name)
          .join(", ")}.`,
      });
    }
  }
  const rank: Record<CheckKind, number> = { "after-failure": 0, overlap: 1, unordered: 2, twice: 3 };
  return out.sort((x, y) => rank[x.kind] - rank[y.kind]);
}

/** Per item: data it waits for, and items that wait for its data. */
export function dataNeighbours(data: DataDependency[] | undefined) {
  const upstream = new Map<string, DataDependency[]>();
  const downstream = new Map<string, DataDependency[]>();
  for (const d of data ?? []) {
    if (!upstream.has(d.to)) upstream.set(d.to, []);
    upstream.get(d.to)!.push(d);
    if (!downstream.has(d.from)) downstream.set(d.from, []);
    downstream.get(d.from)!.push(d);
  }
  return { upstream, downstream };
}
