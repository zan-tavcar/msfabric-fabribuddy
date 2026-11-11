import { useEffect, useMemo } from "react";
import {
  AlertOctagon,
  CalendarClock,
  CalendarDays,
  GanttChart,
  GitFork,
  LayoutList,
} from "lucide-react";
import { Empty, Loading } from "../components/primitives";
import type { JobsPayload, LivePayload, RunsPayload } from "./api";
import { keepJob, type Filters } from "./filters";
import { monitorStore, useMonitor } from "./store";
import { JobDetail } from "./views/JobDetail";
import { CalendarPage } from "./views/CalendarPage";
import { Chains } from "./views/Chains";
import { Failures } from "./views/Failures";
import { Jobs } from "./views/Jobs";
import { Schedules } from "./views/Schedules";
import { Timeline } from "./views/Timeline";
import "./monitor.css";

export type MonitorView = "items" | "gantt" | "chains" | "calendar" | "schedules" | "failures";

export const MONITOR_NAV: { id: MonitorView; label: string; icon: typeof LayoutList }[] = [
  { id: "items", label: "Items", icon: LayoutList },
  { id: "gantt", label: "Gantt", icon: GanttChart },
  { id: "chains", label: "Dependencies", icon: GitFork },
  { id: "calendar", label: "Calendar", icon: CalendarDays },
  { id: "schedules", label: "Schedules", icon: CalendarClock },
  { id: "failures", label: "Failures", icon: AlertOctagon },
];

export { MonitorTree } from "./components/MonitorTree";
export { LiveBadge } from "./components/Live";

/** What the shared top bar shows while the monitor is open. */
export interface MonitorMeta {
  generatedAt: string | null;
  items: number | null;
  runs: number | null;
}

/** The lists, narrowed to the items the shared filters keep. */
function scope(
  filters: Filters,
  jobs: JobsPayload,
  runs: RunsPayload | null,
  live: LivePayload | null,
): { jobs: JobsPayload; runs: RunsPayload | null; live: LivePayload | null } {
  const kept = jobs.jobs.filter((j) => keepJob(j, filters));
  if (kept.length === jobs.jobs.length) return { jobs, runs, live };
  const ids = new Set(kept.map((j) => j.id));
  return {
    jobs: { ...jobs, jobs: kept },
    runs: runs && { ...runs, runs: runs.runs.filter((r) => ids.has(r.item.id)) },
    live: live && {
      ...live,
      running: live.running.filter((r) => ids.has(r.item.id)),
      recent: live.recent.filter((r) => ids.has(r.item.id)),
      upcoming: live.upcoming.filter((r) => ids.has(r.item.id)),
    },
  };
}

/**
 * FabriBuddy Job Monitor: every runnable item (pipelines, notebooks,
 * dataflows, semantic models), its runs, what each run executed and how long
 * each child took, the chains of what runs what, schedules, failures and code.
 * Live when FabriBuddy_Live runs; otherwise what the last scan read.
 */
export function MonitorApp({
  view,
  onNavigate,
  onMeta,
}: {
  view: MonitorView;
  onNavigate: (view: MonitorView) => void;
  onMeta: (meta: MonitorMeta) => void;
}) {
  const state = useMonitor();
  const { jobs, runs, live, error, filters, open } = state;

  useEffect(() => {
    onMeta({
      generatedAt: live?.generatedAt ?? jobs?.generatedAt ?? null,
      items: jobs?.jobs.length ?? null,
      runs: runs?.runs.length ?? null,
    });
  }, [jobs, runs, live, onMeta]);

  // A newly opened page starts at the top.
  useEffect(() => {
    document.querySelector(".content")?.scrollTo?.({ top: 0 });
  }, [open?.id, view]);

  const scoped = useMemo(
    () => (jobs ? scope(filters, jobs, runs, live) : null),
    [filters, jobs, runs, live],
  );
  const running = useMemo(() => new Set((live?.running ?? []).map((r) => r.item.id)), [live]);

  if (error) return <p className="banner banner-danger">{error}</p>;
  if (!jobs || !scoped) return <Loading what="the Job Monitor" />;
  if (!jobs.jobs.length) {
    return (
      <Empty title="No runnable items yet">
        Pipelines, notebooks, dataflows and semantic models appear here after a scan, with their run history.
        Run history is read from workspaces where the service principal is a Contributor.
      </Empty>
    );
  }

  const openItem = (id: string, runId?: string) => monitorStore.openItem(id, runId);
  let page: React.ReactNode;
  if (open) {
    page = (
      <JobDetail
        key={open.id}
        itemId={open.id}
        runId={open.runId}
        tab={open.tab}
        onTab={monitorStore.setTab}
        onRun={monitorStore.setRun}
        onOpen={openItem}
        onBack={monitorStore.close}
      />
    );
  } else if (view === "items") {
    page = <Jobs all={jobs} data={scoped.jobs} runs={scoped.runs} running={running} onOpen={openItem} />;
  } else if (view === "chains") {
    page = <Chains all={jobs} jobs={scoped.jobs} runs={scoped.runs} live={scoped.live} onOpen={openItem} />;
  } else if (view === "schedules") {
    page = <Schedules all={jobs} jobs={scoped.jobs} onOpen={openItem} />;
  } else if (!scoped.runs) {
    page = <Loading what="the runs" />;
  } else if (view === "gantt") {
    page = <Timeline all={jobs} runs={scoped.runs} live={scoped.live} onOpen={openItem} />;
  } else if (view === "calendar") {
    page = <CalendarPage all={jobs} jobs={scoped.jobs} runs={scoped.runs} live={scoped.live} onOpen={openItem} />;
  } else {
    page = <Failures all={jobs} runs={scoped.runs} onOpen={openItem} />;
  }

  return (
    <div className="mon-root">
      {page}
    </div>
  );
}
