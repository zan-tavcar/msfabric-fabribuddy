/**
 * The Job Monitor's shared state: the lists every page reads, the live
 * answer it polls, which item is open and which workspace the sidebar scopes
 * to. One store, so the sidebar tree (rendered by App) and the pages agree.
 *
 * Live: every POLL_SECONDS the small `/monitor/live` answer is read again,
 * skipping every cache. When it moved (FabriBuddy_Live rewrote it), the lists
 * are read again and `version` goes up, so an open item page re-reads too.
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import { ApiError } from "../api/client";
import { monitorApi, type JobPayload, type JobsPayload, type LivePayload, type RunsPayload } from "./api";
import { NO_FILTERS, type Filters } from "./filters";

export const POLL_SECONDS = 30;

export type DetailTab = "overview" | "runs" | "graph" | "gantt" | "calendar" | "code";

export interface OpenItem {
  id: string;
  runId: string | null;
  tab: DetailTab;
}

export interface MonitorState {
  jobs: JobsPayload | null;
  runs: RunsPayload | null;
  /** Null when the stored scan predates the live answer. */
  live: LivePayload | null;
  error: string | null;
  /** Goes up each time the live answer moved and the lists were re-read. */
  version: number;
  /** When the live answer was last checked (ms). */
  checkedAt: number | null;
  /** The filters every page shares (the sidebar's workspace is one of them). */
  filters: Filters;
  open: OpenItem | null;
}

let state: MonitorState = {
  jobs: null,
  runs: null,
  live: null,
  error: null,
  version: 0,
  checkedAt: null,
  filters: loadFilters(),
  open: null,
};
const listeners = new Set<() => void>();

const FILTERS_KEY = "fabribuddy:monitor-filters";

/** The time range is remembered per viewer; the rest starts clear. */
function loadFilters(): Filters {
  try {
    const range = window.localStorage.getItem(FILTERS_KEY);
    if (range === "6h" || range === "24h" || range === "7d" || range === "30d") return { ...NO_FILTERS, range };
  } catch {
    // storage unavailable
  }
  return NO_FILTERS;
}

function set(patch: Partial<MonitorState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

let started = false;
let timer: ReturnType<typeof setInterval> | null = null;
let polling = false;

async function load() {
  try {
    const [jobs, runs, live] = await Promise.all([
      monitorApi.jobs(),
      monitorApi.runs().catch(() => null),
      monitorApi.live().catch(() => null),
    ]);
    set({ jobs, runs, live, checkedAt: Date.now() });
  } catch (e) {
    set({ error: e instanceof ApiError ? e.message : String(e) });
  }
}

/** Read the live answer again; when it moved, the lists too. */
export async function poll() {
  if (polling || !state.jobs) return;
  polling = true;
  try {
    const live = await monitorApi.live(true);
    if (!state.live || live.generatedAt !== state.live.generatedAt) {
      const [jobs, runs] = await Promise.all([monitorApi.jobs(true), monitorApi.runs(true)]);
      set({ jobs, runs, live, version: state.version + 1, checkedAt: Date.now() });
    } else {
      set({ live, checkedAt: Date.now() });
    }
  } catch {
    // An older scan has no live answer, or the connection blinked: keep what
    // is shown, try again next time.
    set({ checkedAt: Date.now() });
  } finally {
    polling = false;
  }
}

function start() {
  if (started) return;
  started = true;
  void load();
  timer = setInterval(() => {
    if (typeof document === "undefined" || !document.hidden) void poll();
  }, POLL_SECONDS * 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void poll();
  });
}

function subscribe(listener: () => void) {
  start();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useMonitor(): MonitorState {
  return useSyncExternalStore(subscribe, () => state);
}

export const monitorStore = {
  get: () => state,
  setFilters: (patch: Partial<Filters>) => {
    if (patch.range) {
      try {
        window.localStorage.setItem(FILTERS_KEY, patch.range);
      } catch {
        // storage unavailable
      }
    }
    set({ filters: { ...state.filters, ...patch } });
  },
  /** The sidebar: one workspace (by name), or all. */
  setWorkspace: (name: string | null) => set({ filters: { ...state.filters, workspaces: name ? [name] : [] } }),
  openItem: (id: string, runId?: string | null, tab?: DetailTab) =>
    set({ open: { id, runId: runId ?? null, tab: tab ?? (runId ? "graph" : "overview") } }),
  close: () => set({ open: null }),
  setTab: (tab: DetailTab) => state.open && set({ open: { ...state.open, tab } }),
  setRun: (runId: string) => state.open && set({ open: { ...state.open, runId } }),
  /** For tests and the dev console. */
  stop: () => {
    if (timer) clearInterval(timer);
    started = false;
  },
};

/** One item's page; read again (fresh) whenever the live answer moved. */
export function useJob(id: string | null): { page: JobPayload | null; error: string | null } {
  const { version } = useMonitor();
  const [page, setPage] = useState<JobPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [shownId, setShownId] = useState<string | null>(null);
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    if (id !== shownId) {
      setPage(null);
      setError(null);
    }
    monitorApi
      .job(id, version > 0)
      .then((p) => {
        if (cancelled) return;
        setPage(p);
        setShownId(id);
      })
      .catch((e) => !cancelled && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, version]);
  return { page: id === shownId ? page : null, error };
}

/** A clock for elapsed times and "in 3m": ticks every `ms`. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
