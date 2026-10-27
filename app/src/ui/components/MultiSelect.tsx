import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";

export interface MultiOption {
  id: string;
  name: string;
  detail?: string;
}

/**
 * A filter that takes several values: a button that reads like the select it
 * replaces ("All workspaces", "nba-storage-prd", "3 workspaces") and opens a
 * checkbox list, searchable once the list is long. Nothing chosen means all.
 */
export function MultiSelect({
  label,
  allLabel,
  noun,
  options,
  value,
  onChange,
  disabled,
}: {
  label: string;
  /** Shown when nothing is chosen, e.g. "All workspaces". */
  allLabel: string;
  /** Plural for the count, e.g. "workspaces". */
  noun: string;
  options: MultiOption[];
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const chosen = new Set(value);
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle
      ? options.filter((o) => `${o.name} ${o.detail ?? ""}`.toLowerCase().includes(needle))
      : options;
  }, [options, query]);

  const summary =
    value.length === 0
      ? allLabel
      : value.length === 1
        ? (options.find((o) => o.id === value[0])?.name ?? `1 ${noun}`)
        : `${value.length} ${noun}`;

  const toggle = (id: string) =>
    onChange(chosen.has(id) ? value.filter((v) => v !== id) : [...value, id]);

  return (
    <div className="filter-field multi" ref={root}>
      <span>{label}</span>
      <button
        type="button"
        className={`input multi-button${value.length ? " multi-active" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="multi-summary">{summary}</span>
        <ChevronDown size={14} strokeWidth={2.2} aria-hidden="true" />
      </button>
      {open && (
        <div className="multi-panel" id={listId} role="listbox" aria-multiselectable="true">
          {options.length > 8 && (
            <label className="multi-search">
              <Search size={13} strokeWidth={2.2} aria-hidden="true" />
              <input
                autoFocus
                value={query}
                placeholder={`Find ${noun}`}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
          )}
          <div className="multi-options">
            {shown.length === 0 && <span className="muted-note multi-empty">No match.</span>}
            {shown.map((o) => (
              <button
                key={o.id}
                type="button"
                role="option"
                aria-selected={chosen.has(o.id)}
                className="multi-option"
                onClick={() => toggle(o.id)}
              >
                <span className={`multi-check${chosen.has(o.id) ? " on" : ""}`}>
                  {chosen.has(o.id) && <Check size={11} strokeWidth={3} aria-hidden="true" />}
                </span>
                <span className="multi-name">{o.name}</span>
                {o.detail && <span className="multi-detail">{o.detail}</span>}
              </button>
            ))}
          </div>
          {value.length > 0 && (
            <button type="button" className="multi-clear" onClick={() => onChange([])}>
              Clear ({value.length})
            </button>
          )}
        </div>
      )}
    </div>
  );
}
