import { Fragment, useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, ChevronUp, ChevronsUpDown, Download, X } from "lucide-react";
import { MultiSelect } from "../../components/MultiSelect";
import { downloadCsv, stamped, toCsv } from "../lib/csv";
import type { Tone } from "../lib/pills";

/* ------------------------------------------------------------------ pills */

export function StatusPill({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

/* ------------------------------------------------------------- filter bar */

export interface FilterDef {
  key: string;
  label: string;
  options: string[];
  /** Chosen values; empty = all, several = any of them. */
  value: string[];
  onChange: (v: string[]) => void;
}

/**
 * One filter row above everything it scopes — never per-card filters, so all
 * tables on a page re-render against the same slice.
 */
export function FilterBar({ filters }: { filters: FilterDef[] }) {
  const active = filters.filter((f) => f.value.length);
  return (
    <div className="filterbar">
      <div className="filterbar-icon">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path
            d="M2 4h12M4 8h8M6 12h4"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
        Filters
      </div>
      <div className="filterbar-fields">
        {filters.map((f) => (
          <div key={f.key} className="filterbar-field">
            <MultiSelect
              label={f.label}
              allLabel="All"
              noun={
                f.label.toLowerCase().endsWith("s")
                  ? f.label.toLowerCase()
                  : `${f.label.toLowerCase()}s`
              }
              options={f.options.map((o) => ({ id: o, name: o }))}
              value={f.value}
              onChange={f.onChange}
            />
          </div>
        ))}
      </div>
      {active.length > 0 && (
        <button
          className="btn btn-ghost filterbar-clear"
          onClick={() => filters.forEach((f) => f.onChange([]))}
        >
          <X size={14} strokeWidth={2.2} />
          Clear {active.length === 1 ? "filter" : `all ${active.length} filters`}
        </button>
      )}
    </div>
  );
}

/* ----------------------------------------------------------- sortable table */

export interface Column<T> {
  key: string;
  label: ReactNode;
  width?: string;
  align?: "right";
  /** Set false for columns with nothing meaningful to order by. */
  sortable?: boolean;
  /** Sort key. Falls back to the rendered text when omitted. */
  value?: (row: T) => string | number | null | undefined;
  render: (row: T) => ReactNode;
}

type Dir = "asc" | "desc";

/**
 * Table with click-to-sort headers and a bounded render.
 *
 * A real tenant has thousands of rows; putting them all in the DOM makes the
 * page crawl and buries what matters. Sorting always applies to the FULL set,
 * not just the visible page — otherwise "sort by oldest" would lie.
 */
function exportRows<T>(columns: Column<T>[], rows: T[]): { headers: string[]; body: string[][] } {
  // Export what the column *sorts* by where available: the rendered cell is
  // JSX (pills, avatars) and cannot go in a spreadsheet.
  const usable = columns.filter((c) => c.value);
  return {
    headers: usable.map((c) => (typeof c.label === "string" ? c.label : c.key)),
    body: rows.map((r) => usable.map((c) => String(c.value!(r) ?? ""))),
  };
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  initialSort,
  initialDir = "asc",
  pageSize = 100,
  dense,
  empty = "Nothing matches that filter.",
  expand,
  canExpand,
  exportName,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  initialSort?: string;
  initialDir?: Dir;
  pageSize?: number;
  dense?: boolean;
  empty?: string;
  /** Renders a nested panel under a row; the row gets an expand caret. */
  expand?: (row: T) => ReactNode;
  /** Suppresses the caret for rows with nothing to expand. */
  canExpand?: (row: T) => boolean;
  /** Enables CSV export of the full (sorted, filtered) row set. */
  exportName?: string;
}) {
  const [sortKey, setSortKey] = useState<string | undefined>(initialSort);
  const [dir, setDir] = useState<Dir>(initialDir);
  const [limit, setLimit] = useState(pageSize);
  const [open, setOpen] = useState<Set<string>>(new Set());

  const toggleOpen = (k: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return rows;
    const get = (r: T) => {
      const v = col.value ? col.value(r) : undefined;
      return v ?? "";
    };
    const factor = dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = get(a);
      const bv = get(b);
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * factor;
      }
      return String(av).localeCompare(String(bv), undefined, { numeric: true }) * factor;
    });
  }, [rows, columns, sortKey, dir]);

  const visible = sorted.slice(0, limit);
  const more = sorted.length - visible.length;

  const toggle = (col: Column<T>) => {
    if (col.sortable === false) return;
    if (sortKey === col.key) {
      setDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(col.key);
      // Numeric and date-ish columns are far more useful largest-first.
      setDir(col.value ? "desc" : "asc");
    }
  };

  const doExport = () => {
    const { headers, body } = exportRows(columns, sorted);
    downloadCsv(stamped(exportName ?? "table"), toCsv(headers, body));
  };

  return (
    <>
      {exportName && (
        <div className="table-toolbar">
          <span className="muted-note">{sorted.length.toLocaleString()} rows</span>
          <button className="btn btn-ghost" onClick={doExport} disabled={sorted.length === 0}>
            <Download size={14} strokeWidth={2.2} />
            Export CSV
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className={dense ? "data-table dense" : "data-table"}>
          <thead>
            <tr>
              {expand && <th style={{ width: 34 }} aria-label="Expand" />}
              {columns.map((c) => {
                const active = sortKey === c.key;
                const canSort = c.sortable !== false;
                return (
                  <th
                    key={c.key}
                    style={{ width: c.width, textAlign: c.align ?? "left" }}
                    className={canSort ? "sortable" : undefined}
                    onClick={canSort ? () => toggle(c) : undefined}
                    aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
                  >
                    <span className="th-inner">
                      {c.label}
                      {canSort && (
                        <span className={active ? "sort-icon active" : "sort-icon"}>
                          {active ? (
                            dir === "asc" ? (
                              <ChevronUp size={13} strokeWidth={2.4} />
                            ) : (
                              <ChevronDown size={13} strokeWidth={2.4} />
                            )
                          ) : (
                            <ChevronsUpDown size={13} strokeWidth={2} />
                          )}
                        </span>
                      )}
                    </span>
                  </th>
                );
              })}
              {onRowClick && <th style={{ width: 40 }} aria-label="Open" />}
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => {
              const k = rowKey(row);
              const expandable = Boolean(expand) && (canExpand?.(row) ?? true);
              const isOpen = open.has(k);
              return (
                <Fragment key={k}>
                  <tr
                    className={onRowClick ? "clickable-row" : undefined}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                  >
                    {expand && (
                      <td
                        className="row-expand"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (expandable) toggleOpen(k);
                        }}
                      >
                        {expandable && (
                          <button
                            className="expand-btn"
                            aria-expanded={isOpen}
                            aria-label={isOpen ? "Collapse" : "Expand"}
                          >
                            {isOpen ? (
                              <ChevronDown size={15} strokeWidth={2.2} />
                            ) : (
                              <ChevronRight size={15} strokeWidth={2.2} />
                            )}
                          </button>
                        )}
                      </td>
                    )}
                    {columns.map((c) => (
                      <td key={c.key} style={{ textAlign: c.align ?? "left" }}>
                        {c.render(row)}
                      </td>
                    ))}
                    {onRowClick && (
                      <td className="row-chevron">
                        <ChevronRight size={16} strokeWidth={2} />
                      </td>
                    )}
                  </tr>
                  {expandable && isOpen && (
                    <tr className="expanded-row">
                      <td colSpan={columns.length + (expand ? 1 : 0) + (onRowClick ? 1 : 0)}>
                        {expand!(row)}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {rows.length === 0 && <p className="muted-note">{empty}</p>}

      {more > 0 && (
        <div className="show-more">
          <span className="muted-note">
            Showing {visible.length.toLocaleString()} of {sorted.length.toLocaleString()}
          </span>
          <button className="btn btn-ghost" onClick={() => setLimit((l) => l + pageSize * 5)}>
            Show {Math.min(more, pageSize * 5).toLocaleString()} more
          </button>
        </div>
      )}
    </>
  );
}
