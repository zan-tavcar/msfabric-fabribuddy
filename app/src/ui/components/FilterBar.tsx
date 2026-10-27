import { useEffect, useMemo, useState } from "react";
import { Search, X } from "lucide-react";
import type { LineageTree } from "../api/types";
import {
  type Filters,
  NO_FILTERS,
  filterOptions,
  isFiltered,
  setFilter,
  setQuery,
} from "../lib/filters";
import { MultiSelect } from "./MultiSelect";

/**
 * Workspace → item → table, shared by the Docs overview and the Lineage graph.
 *
 * Each level takes several values and narrows the next; a lower choice that no
 * longer belongs to the levels above is dropped.
 */
export function FilterBar({
  tree,
  filters,
  onChange,
}: {
  tree: LineageTree;
  filters: Filters;
  onChange: (next: Filters) => void;
}) {
  const options = useMemo(() => filterOptions(tree, filters), [tree, filters]);

  // Typing is local; the search is applied after a pause, so each keystroke
  // does not refetch the graph.
  const [text, setText] = useState(filters.q);
  useEffect(() => setText(filters.q), [filters.q]);
  useEffect(() => {
    if (text === filters.q) return;
    const timer = setTimeout(() => onChange(setQuery(filters, text)), 350);
    return () => clearTimeout(timer);
  }, [text, filters, onChange]);

  return (
    <div className="toolbar filter-bar" role="group" aria-label="Filters">
      <label htmlFor="f-q" className="filter-field filter-search">
        <span>Search</span>
        <span className="filter-search-box">
          <Search size={14} strokeWidth={2.2} aria-hidden="true" />
          <input
            id="f-q"
            className="input"
            type="search"
            value={text}
            placeholder="Table, schema.table or path"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onChange(setQuery(filters, text));
            }}
          />
        </span>
      </label>
      <MultiSelect
        label="Workspace"
        allLabel="All workspaces"
        noun="workspaces"
        options={options.workspaces}
        value={filters.workspace}
        onChange={(v) => onChange(setFilter(tree, filters, "workspace", v))}
      />
      <MultiSelect
        label="Item"
        allLabel="All items"
        noun="items"
        options={options.items}
        value={filters.item}
        onChange={(v) => onChange(setFilter(tree, filters, "item", v))}
      />
      <MultiSelect
        label="Table"
        allLabel={options.tables.length ? "All tables" : "Pick a workspace or item first"}
        noun="tables"
        options={options.tables}
        value={filters.table}
        disabled={options.tables.length === 0}
        onChange={(v) => onChange(setFilter(tree, filters, "table", v))}
      />
      {isFiltered(filters) && (
        <button type="button" className="btn" onClick={() => onChange(NO_FILTERS)}>
          <X size={14} strokeWidth={2} aria-hidden="true" />
          Clear
        </button>
      )}
    </div>
  );
}
