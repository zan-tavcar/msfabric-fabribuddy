import { useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  OctagonAlert,
} from "lucide-react";
import type { Severity } from "../lib/insights";

/* ------------------------------------------------------------------ card */

export function Card({
  title,
  subtitle,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card panel ${className}`}>
      {(title || right) && (
        <header className="panel-head">
          <div>
            {title && <h2 className="panel-title">{title}</h2>}
            {subtitle && <p className="panel-sub">{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

/* ---------------------------------------------------------------- status */

/**
 * Status never rides on color alone — every severity ships an icon and a
 * label, which is also what makes the sub-3:1 light-mode warning tone legal.
 */
export const SEVERITY_ICON: Record<Severity, typeof Info> = {
  critical: OctagonAlert,
  serious: AlertTriangle,
  warning: Info,
  good: CheckCircle2,
};

export const SEVERITY_TEXT: Record<Severity, string> = {
  critical: "Critical",
  serious: "Needs attention",
  warning: "Worth a look",
  good: "Healthy",
};

export function SeverityChip({ severity }: { severity: Severity }) {
  const Icon = SEVERITY_ICON[severity];
  return (
    <span className={`chip chip-${severity}`}>
      <Icon size={13} strokeWidth={2.2} aria-hidden="true" />
      {SEVERITY_TEXT[severity]}
    </span>
  );
}

export function Tag({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "good" | "warning" | "critical";
}) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

/* ----------------------------------------------------------------- tiles */

export function StatTile({
  label,
  value,
  caption,
  tone,
  trend,
}: {
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  tone?: "good" | "warning" | "critical";
  trend?: ReactNode;
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` stat-${tone}` : ""}`}>{value}</div>
      <div className="stat-foot">
        {caption && <span className="stat-caption">{caption}</span>}
        {trend}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- tables */

export function Table({
  columns,
  children,
  dense,
}: {
  columns: { key: string; label: ReactNode; align?: "right"; width?: string }[];
  children: ReactNode;
  dense?: boolean;
}) {
  return (
    <div className="table-wrap">
      <table className={dense ? "data-table dense" : "data-table"}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{ width: c.width, textAlign: c.align ?? "left" }}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/**
 * Renders a bounded slice of a long list with a "show more" control.
 *
 * A real tenant has thousands of reports; putting every row in the DOM at once
 * makes the page crawl and buries the rows that matter. The filter above the
 * table is the way to reach a specific item — this just keeps the default
 * render honest about how much it is showing.
 */
export function useCapped<T>(items: T[], step = 100) {
  const [limit, setLimit] = useState(step);
  const visible = items.slice(0, limit);
  const more = items.length - visible.length;
  const control =
    more > 0 ? (
      <div className="show-more">
        <span className="muted-note">
          Showing {visible.length.toLocaleString()} of{" "}
          {items.length.toLocaleString()}
        </span>
        <button className="btn btn-ghost" onClick={() => setLimit((l) => l + step * 5)}>
          Show {Math.min(more, step * 5).toLocaleString()} more
        </button>
      </div>
    ) : null;
  return { visible, control, total: items.length };
}

/* ----------------------------------------------------------------- other */

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}

export function UnavailableNote({ reason }: { reason: string }) {
  return (
    <div className="unavailable">
      <Info size={15} strokeWidth={2.2} aria-hidden="true" />
      <div>
        <strong>Not available for this tenant.</strong>
        <p>{reason}</p>
      </div>
    </div>
  );
}

export function StatusTag({ ok, children }: { ok: boolean; children: ReactNode }) {
  return <span className={`tag ${ok ? "tag-good" : "tag-warning"}`}>{children}</span>;
}

export function Avatar({ initials }: { initials: string }) {
  return <span className="avatar">{initials}</span>;
}
