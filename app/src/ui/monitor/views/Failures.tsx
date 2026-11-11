import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { JobsPayload, ListedRun, RunsPayload } from "../api";
import { MonitorFilters } from "../components/MonitorFilters";
import { rangeMs } from "../filters";
import { StatusBadge } from "../components/Status";
import { TypeIcon } from "../components/TypeIcon";
import { errorSignature, relative, when } from "../lib";
import { useMonitor, useNow } from "../store";


interface Group {
  key: string;
  item: ListedRun["item"];
  workspace: string;
  signature: string;
  runs: ListedRun[];
}

/**
 * Failures grouped by item and error, so a dataflow failing the same way ten
 * nights running is one row with "10×", not ten.
 */
export function Failures({
  all,
  runs,
  onOpen,
}: {
  all: JobsPayload;
  runs: RunsPayload;
  onOpen: (id: string, runId?: string) => void;
}) {
  const { filters } = useMonitor();
  const now = useNow(60_000);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const groups = useMemo(() => {
    const since = now - rangeMs(filters.range);
    const by = new Map<string, Group>();
    for (const r of runs.runs) {
      if (r.status !== "failed" || !r.startedAt || new Date(r.startedAt).getTime() < since) continue;
      const signature = errorSignature(r.error);
      const key = `${r.item.id}|${signature}`;
      const g = by.get(key) ?? { key, item: r.item, workspace: r.workspace, signature, runs: [] };
      g.runs.push(r);
      by.set(key, g);
    }
    return [...by.values()].sort((a, b) => (b.runs[0].startedAt ?? "").localeCompare(a.runs[0].startedAt ?? ""));
  }, [runs, filters.range, now]);
  const total = groups.reduce((s, g) => s + g.runs.length, 0);

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Failures</h2>
        <p className="page-head-sub">
          {total} failed run{total === 1 ? "" : "s"} in {groups.length} distinct problem{groups.length === 1 ? "" : "s"}. Alike
          errors are grouped; the newest first.
        </p>
      </header>
      <MonitorFilters jobs={all} count={`${total} failed run${total === 1 ? "" : "s"}`} />
      {groups.length === 0 && (
        <p className="card mon-pad muted-note">
          {filters.statuses.length && !filters.statuses.includes("Failed") ? "The status filter hides failures." : "No failed runs in this range."}
        </p>
      )}
      {groups.map((g) => {
        const isOpen = openKey === g.key;
        const latest = g.runs[0];
        const first = g.runs[g.runs.length - 1];
        return (
          <section key={g.key} className="card mon-fail-group">
            <button type="button" className="mon-fail-head" onClick={() => setOpenKey(isOpen ? null : g.key)} aria-expanded={isOpen}>
              {isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              <TypeIcon type={g.item.type} />
              <span className="mon-fail-title">
                <strong>{g.item.name}</strong>
                <span className="muted-note">
                  {g.workspace} · last {relative(latest.startedAt, now)} · first {when(first.startedAt).slice(0, 16)}
                </span>
              </span>
              <span className="mon-fail-count tnum">{g.runs.length}×</span>
            </button>
            <pre className="mon-error-box">{latest.error || "Fabric reported no message."}</pre>
            {isOpen && (
              <ul className="mon-feed">
                {g.runs.map((r) => (
                  <li key={r.runId}>
                    <button type="button" className="mon-feed-row" onClick={() => onOpen(r.item.id, r.runId)}>
                      <StatusBadge status={r.status} compact />
                      <span className="mon-feed-main">
                        <strong>{when(r.startedAt)}</strong>
                        <span className="muted-note">
                          {r.trigger}
                          {r.parent ? ` · part of a ${r.parent.name} run` : ""}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}
