import { Radio } from "lucide-react";
import type { LivePayload } from "../api";
import { duration, elapsedSec, liveState, when } from "../lib";
import { POLL_SECONDS, poll, useMonitor, useNow } from "../store";
import { StatusBadge } from "./Status";
import { TypeIcon } from "./TypeIcon";

/** Live (pulsing), paused, or the last scan's snapshot — with how to go live. */
export function LiveBadge({ compact }: { compact?: boolean }) {
  const { live, jobs, checkedAt } = useMonitor();
  const now = useNow(1000);
  const s = liveState(live, jobs?.generatedAt ?? null, now);
  const help =
    s.kind === "live"
      ? `FabriBuddy_Live rewrites the monitor every ${live?.source.everySeconds ?? 60}s; this page checks every ${POLL_SECONDS}s.`
      : s.kind === "stale"
        ? "FabriBuddy_Live stopped writing. Check its run history, or its hourly schedule."
        : "What the last scan read. Schedule FabriBuddy_Live (Deploy_FabriBuddy: LIVE_MONITOR = True) to see runs as they happen.";
  return (
    <button
      type="button"
      className={`mon-live mon-live-${s.kind}${compact ? " compact" : ""}`}
      title={`${help}${checkedAt ? ` Checked ${when(new Date(checkedAt).toISOString())}.` : ""} Click to check now.`}
      onClick={() => void poll()}
    >
      <span className="mon-live-dot" aria-hidden="true" />
      <strong>{s.label}</strong>
      <span className="mon-live-detail">{s.detail}</span>
    </button>
  );
}

/** What is running now: elapsed (ticking), the usual duration, the step it is on. */
export function RunningNow({
  live,
  onOpen,
  empty = "Nothing is running.",
}: {
  live: LivePayload | null;
  onOpen: (id: string, runId?: string) => void;
  empty?: string;
}) {
  const now = useNow(1000);
  const running = live?.running ?? [];
  if (!running.length) {
    return (
      <p className="muted-note mon-running-empty">
        <Radio size={14} aria-hidden="true" /> {empty}
      </p>
    );
  }
  // Children sit under the run that started them.
  const ids = new Set(running.map((r) => r.runId));
  const roots = running.filter((r) => !(r.parent?.runId && ids.has(r.parent.runId)));
  const kids = (id: string) => running.filter((r) => r.parent?.runId === id);
  const row = (r: (typeof running)[number], depth: number): React.ReactNode => {
    const spent = elapsedSec(r.startedAt, now) ?? 0;
    const usual = r.expectedSec;
    const share = usual ? Math.min(1, spent / usual) : null;
    const over = usual != null && spent > usual * 1.25;
    return (
      <div key={r.runId}>
        <button type="button" className="mon-running-row" style={{ paddingLeft: 12 + depth * 22 }} onClick={() => onOpen(r.item.id, r.runId)}>
          <TypeIcon type={r.item.type} />
          <span className="mon-running-name">
            <strong>{r.item.name}</strong>
            <span className="muted-note">
              {r.trigger} · started {when(r.startedAt).slice(11)}
              {r.activeSteps.length ? ` · now: ${r.activeSteps.join(", ")}` : ""}
            </span>
          </span>
          <span className="mon-running-progress" aria-hidden="true">
            <span
              className={`mon-running-fill${over ? " over" : ""}${share == null ? " unknown" : ""}`}
              style={{ width: `${share == null ? 100 : Math.max(4, share * 100)}%` }}
            />
          </span>
          <span className="mon-running-time tnum">
            <strong>{duration(spent)}</strong>
            <span className="muted-note">
              {usual == null
                ? "no history yet"
                : over
                  ? `${duration(spent - usual)} over the usual ${duration(usual)}`
                  : `~${duration(Math.max(usual - spent, 0))} left · usual ${duration(usual)}`}
            </span>
          </span>
          <StatusBadge status={r.status} />
        </button>
        {kids(r.runId).map((k) => row(k, depth + 1))}
      </div>
    );
  };
  return <div className="mon-runlist">{roots.map((r) => row(r, 0))}</div>;
}
