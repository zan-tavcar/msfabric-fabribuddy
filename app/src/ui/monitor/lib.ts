import type { RunStatus } from "./api";

export const STATUS_LABEL: Record<RunStatus, string> = {
  success: "Succeeded",
  failed: "Failed",
  running: "In progress",
  queued: "Queued",
  cancelled: "Cancelled",
  skipped: "Skipped",
  unknown: "Unknown",
};

/** 1h 02m · 27m 13s · 48s · 0.8s */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(r).padStart(2, "0")}s`;
  return `${r}s`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** 2026-11-11 02:00:04, in the viewer's time zone. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function shortWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "in 3h 12m" / "5m ago" relative to now. */
export function relative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const diff = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(diff);
  const text =
    abs >= 2 * 86_400
      ? `${Math.floor(abs / 86_400)}d ${Math.floor((abs % 86_400) / 3600)}h`
      : duration(abs).replace(/\.\ds$/, "s");
  return diff >= 0 ? `in ${text}` : `${text} ago`;
}

/** The first scheduled run still in the future (the payload lists them from scan time). */
export function nextRun(upcoming: string[] | undefined, now = Date.now()): string | null {
  return (upcoming ?? []).find((u) => new Date(u).getTime() > now) ?? null;
}

export const shortId = (id: string) => id.slice(0, 8);

/**
 * Layered layout for a DAG: each node's column is its longest path from a root,
 * rows are ordered to keep a node near its parents (one barycentre pass).
 */
export function layoutDag(
  ids: string[],
  edges: { from: string; to: string }[],
): Map<string, { col: number; row: number }> {
  const parents = new Map<string, string[]>();
  const children = new Map<string, string[]>();
  for (const id of ids) {
    parents.set(id, []);
    children.set(id, []);
  }
  for (const e of edges) {
    if (!parents.has(e.to) || !children.has(e.from) || e.from === e.to) continue;
    parents.get(e.to)!.push(e.from);
    children.get(e.from)!.push(e.to);
  }
  const col = new Map<string, number>();
  const visiting = new Set<string>();
  const depth = (id: string): number => {
    if (col.has(id)) return col.get(id)!;
    if (visiting.has(id)) return 0; // a cycle: break it
    visiting.add(id);
    const ps = parents.get(id) ?? [];
    const d = ps.length ? Math.max(...ps.map(depth)) + 1 : 0;
    visiting.delete(id);
    col.set(id, d);
    return d;
  };
  ids.forEach(depth);
  const columns: string[][] = [];
  for (const id of ids) {
    const c = col.get(id)!;
    (columns[c] ??= []).push(id);
  }
  const row = new Map<string, number>();
  columns.forEach((members, c) => {
    if (c > 0) {
      const centre = (id: string) => {
        const ps = parents.get(id)!.filter((p) => row.has(p));
        return ps.length ? ps.reduce((s, p) => s + row.get(p)!, 0) / ps.length : 0;
      };
      members.sort((a, b) => centre(a) - centre(b));
    }
    members.forEach((id, i) => row.set(id, i));
  });
  const out = new Map<string, { col: number; row: number }>();
  for (const id of ids) out.set(id, { col: col.get(id)!, row: row.get(id)! });
  return out;
}

/** Seconds since `iso` (a running run's elapsed time). */
export function elapsedSec(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  return Math.max(0, (now - new Date(iso).getTime()) / 1000);
}

/** A run's duration, or — while it runs — its time so far. */
export function runSeconds(r: { startedAt: string | null; endedAt: string | null; durationSec: number | null; status: RunStatus }, now = Date.now()): number | null {
  if (r.durationSec != null) return r.durationSec;
  if (r.status === "running" && r.startedAt) return elapsedSec(r.startedAt, now);
  return null;
}

export type LiveState =
  | { kind: "live"; label: string; detail: string }
  | { kind: "stale"; label: string; detail: string }
  | { kind: "snapshot"; label: string; detail: string };

/** Is the monitor live (FabriBuddy_Live writing every minute), stalled, or a scan's snapshot? */
export function liveState(
  live: { generatedAt: string; source: { kind: string; everySeconds: number | null } } | null,
  fallbackAt: string | null,
  now = Date.now(),
): LiveState {
  const at = live?.generatedAt ?? fallbackAt;
  const age = at ? (now - new Date(at).getTime()) / 1000 : null;
  const ago = age == null ? "" : age < 90 ? `${Math.max(1, Math.round(age))}s ago` : `${duration(age).replace(/ \d+s$/, "")} ago`;
  if (live?.source.kind === "live") {
    const every = live.source.everySeconds ?? 60;
    if (age != null && age < every * 3 + 30) {
      return { kind: "live", label: "Live", detail: `updated ${ago} · every ${every}s` };
    }
    return { kind: "stale", label: "Live paused", detail: `last update ${ago} · FabriBuddy_Live isn't running` };
  }
  return { kind: "snapshot", label: "Last scan", detail: at ? `${when(at)} · ${ago}` : "—" };
}

/** Day key in the viewer's time zone: 2026-11-11. */
export function dayKey(iso: string | number | Date): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The first line of an error, without run ids and times, to group alike failures. */
export function errorSignature(error: string): string {
  return (error || "Failed without a message")
    .split("\n")[0]
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, "…")
    .replace(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?( UTC|Z)?/g, "…")
    .slice(0, 220);
}

export const DAY_MS = 86_400_000;
