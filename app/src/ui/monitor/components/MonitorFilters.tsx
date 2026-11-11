import { useEffect, useState, type ReactNode } from "react";
import { Search } from "lucide-react";
import type { JobsPayload } from "../api";
import { FilterBar } from "../../audience/components/DataTable";
import { RANGES, ROLE, type Filters } from "../filters";
import { STATUS_LABEL } from "../lib";
import { monitorStore, useMonitor } from "../store";

/**
 * The same filters on every page: workspace, item type, item, status, role in
 * a chain; then the count, the time range, a keyword, and the page's own
 * controls (sort, layout). Picks are shared, so they follow you between pages.
 */
export function MonitorFilters({
  jobs,
  count,
  statusLabel = "Run status",
  ranges = "last",
  children,
}: {
  jobs: JobsPayload;
  /** "12 items", "340 runs". */
  count?: ReactNode;
  /** "Last run status" on Items; "Run status" where each run is a row. */
  statusLabel?: string;
  /** "last" (history), "next" (what the schedules will start), or none. */
  ranges?: "last" | "next" | false;
  children?: ReactNode;
}) {
  const { filters } = useMonitor();
  const setF = (patch: Partial<Filters>) => monitorStore.setFilters(patch);
  const uniq = (xs: string[]) => [...new Set(xs)].sort();

  // Typing is local; the filter applies after a short pause.
  const [text, setText] = useState(filters.q);
  useEffect(() => setText(filters.q), [filters.q]);
  useEffect(() => {
    if (text === filters.q) return;
    const t = setTimeout(() => setF({ q: text }), 200);
    return () => clearTimeout(t);
  }, [text, filters.q]); // eslint-disable-line react-hooks/exhaustive-deps

  // Item names, narrowed to the workspaces and types already picked.
  const itemNames = uniq(
    jobs.jobs
      .filter(
        (j) =>
          !filters.workspaces.length ||
          filters.workspaces.includes(j.workspace),
      )
      .filter(
        (j) => !filters.types.length || filters.types.includes(j.typeLabel),
      )
      .map((j) => j.name),
  );
  const statuses = [
    ...Object.values(STATUS_LABEL).filter((s) => s !== STATUS_LABEL.unknown),
    "Never run",
  ];

  return (
    <>
      <div className="mon-filters">
        <FilterBar
          filters={[
            {
              key: "ws",
              label: "Workspace",
              options: uniq(jobs.jobs.map((j) => j.workspace)),
              value: filters.workspaces,
              onChange: (v) => setF({ workspaces: v }),
            },
            {
              key: "type",
              label: "Item type",
              options: uniq(jobs.jobs.map((j) => j.typeLabel)),
              value: filters.types,
              onChange: (v) => setF({ types: v }),
            },
            {
              key: "item",
              label: "Item",
              options: itemNames,
              value: filters.items,
              onChange: (v) => setF({ items: v }),
            },
            {
              key: "status",
              label: statusLabel,
              options: statuses,
              value: filters.statuses,
              onChange: (v) => setF({ statuses: v }),
            },
            {
              key: "role",
              label: "Role in chain",
              options: Object.values(ROLE),
              value: filters.roles,
              onChange: (v) => setF({ roles: v }),
            },
          ]}
        />
      </div>
      <div className="mon-toolbar">
        {count != null && <strong className="mon-count">{count}</strong>}
        {ranges && (
          <div className="segmented" role="group" aria-label="Time range">
            {RANGES.map((r) => (
              <button
                key={r.id}
                type="button"
                className={filters.range === r.id ? "active" : ""}
                onClick={() => setF({ range: r.id })}
              >
                {ranges === "next" ? r.next : r.label}
              </button>
            ))}
          </div>
        )}
        <label className="mon-search">
          <Search size={14} aria-hidden="true" />
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Filter by keyword"
            aria-label="Filter by keyword"
          />
        </label>
        {children}
      </div>
    </>
  );
}
