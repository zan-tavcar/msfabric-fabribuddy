import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Folder, Layers, Search } from "lucide-react";
import type { JobSummary } from "../api";
import { STATUS_LABEL } from "../lib";
import { monitorStore, useMonitor } from "../store";
import { TypeIcon } from "./TypeIcon";

const GROUP: Record<string, string> = {
  DataPipeline: "Pipelines",
  Notebook: "Notebooks",
  Dataflow: "Dataflows",
  SemanticModel: "Semantic models",
  SparkJobDefinition: "Spark job definitions",
  CopyJob: "Copy jobs",
};

/**
 * The sidebar while the monitor is open, like the Data Catalog's: workspaces,
 * their runnable items by type. A workspace scopes every page to it (click
 * again for all); an item opens. Dots are each item's latest run; a pulse is
 * running now.
 */
export function MonitorTree() {
  const { jobs, live, filters, open } = useMonitor();
  const scoped = filters.workspaces.length === 1 ? filters.workspaces[0] : null;
  const [q, setQ] = useState("");
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const running = useMemo(() => new Set((live?.running ?? []).map((r) => r.item.id)), [live]);

  const workspaces = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const by = new Map<string, { id: string; name: string; groups: Map<string, JobSummary[]> }>();
    for (const j of jobs?.jobs ?? []) {
      if (needle && !`${j.name} ${j.workspace}`.toLowerCase().includes(needle)) continue;
      const w = by.get(j.workspaceId) ?? { id: j.workspaceId, name: j.workspace, groups: new Map() };
      const g = w.groups.get(j.type) ?? [];
      g.push(j);
      w.groups.set(j.type, g);
      by.set(j.workspaceId, w);
    }
    return [...by.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [jobs, q]);

  if (!jobs) return null;
  const toggle = (id: string) =>
    setClosed((c) => {
      const next = new Set(c);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const isOpen = (id: string) => Boolean(q) || !closed.has(id);

  return (
    <>
      <div className="tree-search">
        <label htmlFor="mon-tree-q">Browse workspaces</label>
        <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
          <Search size={14} strokeWidth={2.2} aria-hidden="true" className="mon-tree-search-icon" />
          <input
            id="mon-tree-q"
            className="input"
            style={{ paddingLeft: 30 }}
            value={q}
            placeholder="Search pipelines, notebooks…"
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>
      <div className="tree-body" role="tree" aria-label="Runnable items">
        <button
          type="button"
          className={`tree-row ws${!filters.workspaces.length ? " active" : ""}`}
          style={{ paddingLeft: 8 }}
          onClick={() => monitorStore.setWorkspace(null)}
          role="treeitem"
          aria-selected={!filters.workspaces.length}
        >
          <span className="tree-chev" />
          <span className="tree-icon"><Layers size={15} strokeWidth={1.7} aria-hidden="true" /></span>
          <span className="tree-label">All workspaces</span>
          <span className="tree-count tnum">{jobs.jobs.length}</span>
        </button>
        {workspaces.map((w) => {
          const all = [...w.groups.values()].flat();
          const failing = all.filter((j) => j.latest?.status === "failed").length;
          const busy = all.some((j) => running.has(j.id));
          const wsOpen = isOpen(w.id);
          return (
            <div key={w.id}>
              <button
                type="button"
                className={`tree-row ws${scoped === w.name ? " active" : ""}`}
                style={{ paddingLeft: 8 }}
                role="treeitem"
                aria-expanded={wsOpen}
                aria-selected={scoped === w.name}
                title="Show only this workspace on every page (click again for all)"
                onClick={() => monitorStore.setWorkspace(scoped === w.name ? null : w.name)}
              >
                <span
                  className="tree-chev"
                  onClick={(e) => {
                    e.stopPropagation();
                    toggle(w.id);
                  }}
                >
                  {wsOpen ? <ChevronDown size={13} strokeWidth={2.4} /> : <ChevronRight size={13} strokeWidth={2.4} />}
                </span>
                <span className="tree-icon"><Folder size={15} strokeWidth={1.7} aria-hidden="true" /></span>
                <span className="tree-label">{w.name}</span>
                {busy && <span className="mon-pulse" title="Running now" />}
                {failing > 0 && (
                  <span className="mon-tree-fail tnum" title={`${failing} item${failing === 1 ? "" : "s"} failing`}>
                    {failing}
                  </span>
                )}
                <span className="tree-count tnum">{all.length}</span>
              </button>
              {wsOpen &&
                [...w.groups.entries()]
                  .sort(([a], [b]) => (GROUP[a] ?? a).localeCompare(GROUP[b] ?? b))
                  .map(([type, items]) => {
                    const gid = `${w.id}/${type}`;
                    const gOpen = isOpen(gid);
                    return (
                      <div key={gid} role="group">
                        <button
                          type="button"
                          className="tree-row"
                          style={{ paddingLeft: 24 }}
                          onClick={() => toggle(gid)}
                          aria-expanded={gOpen}
                        >
                          <span className="tree-chev">
                            {gOpen ? <ChevronDown size={13} strokeWidth={2.4} /> : <ChevronRight size={13} strokeWidth={2.4} />}
                          </span>
                          <span className="tree-icon"><TypeIcon type={type} size={13} /></span>
                          <span className="tree-label">{GROUP[type] ?? type}</span>
                          <span className="tree-count tnum">{items.length}</span>
                        </button>
                        {gOpen &&
                          [...items]
                            .sort((a, b) => a.name.localeCompare(b.name))
                            .map((j) => {
                              const state = running.has(j.id) ? "running" : j.latest?.status ?? "none";
                              return (
                                <button
                                  key={j.id}
                                  type="button"
                                  className={`tree-row${open?.id === j.id ? " active" : ""}`}
                                  style={{ paddingLeft: 56 }}
                                  role="treeitem"
                                  aria-selected={open?.id === j.id}
                                  onClick={() => monitorStore.openItem(j.id)}
                                  title={`${j.name} — latest run: ${state === "none" ? "never run" : STATUS_LABEL[state as keyof typeof STATUS_LABEL]}`}
                                >
                                  <span className={`mon-tree-dot mon-${state}`} aria-hidden="true" />
                                  <span className="tree-label">{j.name}</span>
                                </button>
                              );
                            })}
                      </div>
                    );
                  })}
            </div>
          );
        })}
        {workspaces.length === 0 && <p className="tree-empty">No item matches.</p>}
      </div>
    </>
  );
}
