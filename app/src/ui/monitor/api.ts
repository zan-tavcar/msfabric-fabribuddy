/**
 * Job Monitor's reads: runnable items, their runs, schedules and chains,
 * as the scan stored them (core: lineage/monitor.py). Never triggers a scan.
 */
import { req, reqFresh } from "../api/client";

export type RunStatus =
  | "success"
  | "failed"
  | "running"
  | "queued"
  | "cancelled"
  | "skipped"
  | "unknown";

export interface ItemRef {
  id: string;
  name: string;
  type: string;
  typeLabel: string;
}

export interface RunRow {
  runId: string;
  status: RunStatus;
  rawStatus: string;
  /** "Scheduled", "Manual", "API" or "Run by pl_master". */
  trigger: string;
  startedAt: string | null;
  endedAt: string | null;
  durationSec: number | null;
  error: string;
  parent: (ItemRef & { runId: string | null }) | null;
}

export interface Bar {
  runId: string;
  status: RunStatus;
  startedAt: string | null;
  durationSec: number | null;
}

export interface Schedule {
  text: string;
  enabled: boolean;
  /** Next occurrences (UTC ISO) from when the scan ran. */
  upcoming: string[];
}

export interface JobSummary extends ItemRef {
  workspace: string;
  workspaceId: string;
  fabricUrl: string;
  fabricDemo: boolean;
  schedule: Schedule | null;
  latest: RunRow | null;
  bars: Bar[];
  stats: {
    runs: number;
    finished: number;
    succeeded: number;
    failed: number;
    successRate: number | null;
    avgSec: number | null;
    maxSec: number | null;
  };
  /** Items that run this one (parsed from their code). */
  parents: ItemRef[];
  /** Items this one runs, and how ("Pipeline activity", "runMultiple", "%run"…). */
  children: (ItemRef & { via: string })[];
  /** Tables it reads and writes ("lh_silver · sales.customers"), from the lineage. */
  reads: string[];
  writes: string[];
}

/** Item `from` writes tables item `to` reads. */
export interface DataDependency {
  from: string;
  to: string;
  tables: string[];
}

export interface JobsPayload {
  generatedAt: string;
  windowDays: number;
  jobs: JobSummary[];
  /** Data dependencies between items (older scans: absent). */
  data?: DataDependency[];
}

export interface RunsPayload {
  generatedAt: string;
  windowDays: number;
  runs: ListedRun[];
}

export interface RunChild {
  key: string;
  parentKey: string | null;
  depth: number;
  name: string;
  activityType: string;
  item: ItemRef | null;
  runId: string | null;
  status: RunStatus;
  startedAt: string | null;
  endedAt: string | null;
  durationSec: number | null;
  error: string;
  dependsOn: string[];
  /** "measured": Fabric reported it. "in-session": a notebook run inside its caller's session. */
  timing: "measured" | "in-session";
}

export interface JobRun extends RunRow {
  children: RunChild[];
}

export interface ChainNode extends ItemRef {
  focus: boolean;
  latest: RunStatus | null;
}

export interface JobPayload {
  generatedAt: string;
  windowDays: number;
  job: JobSummary;
  chain: { nodes: ChainNode[]; edges: { from: string; to: string; via: string }[] };
  runs: JobRun[];
  code: { partPath: string; language: string; text: string }[];
}

export type ListedRun = RunRow & { item: ItemRef; workspace: string; workspaceId: string };

export interface LivePayload {
  generatedAt: string;
  /** "scan": written once by the last scan. "live": rewritten by FabriBuddy_Live every `everySeconds`. */
  source: { kind: "scan" | "live"; everySeconds: number | null; until?: string };
  running: (ListedRun & {
    /** The item's average successful run, to estimate what is left. */
    expectedSec: number | null;
    /** The steps (activities) running inside it now. */
    activeSteps: string[];
    doneSteps: number;
  })[];
  recent: ListedRun[];
  upcoming: { at: string; item: ItemRef; workspace: string; workspaceId: string; expectedSec: number | null }[];
}

const jobPath = (id: string) => `/monitor/jobs/${encodeURIComponent(id)}`;

export const monitorApi = {
  jobs: (fresh = false) => (fresh ? reqFresh : req)<JobsPayload>("/monitor/jobs"),
  runs: (fresh = false) => (fresh ? reqFresh : req)<RunsPayload>("/monitor/runs"),
  live: (fresh = false) => (fresh ? reqFresh : req)<LivePayload>("/monitor/live"),
  job: (id: string, fresh = false) => (fresh ? reqFresh : req)<JobPayload>(jobPath(id)),
};
