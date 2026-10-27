import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";
import type { Layer } from "../api/types";
import { LAYER_LABEL } from "../lib/layers";

export function Chip({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}

export function LayerChip({ layer, label }: { layer: Layer; label?: string }) {
  return (
    <span className="chip chip-neutral">
      <span className={`layer-dot layer-${layer}`} aria-hidden="true" />
      {label ?? LAYER_LABEL[layer]}
    </span>
  );
}

export function LayerDot({ layer }: { layer: Layer }) {
  return (
    <span
      className={`layer-dot layer-${layer}`}
      title={LAYER_LABEL[layer]}
      aria-label={LAYER_LABEL[layer]}
    />
  );
}

export function Stat({
  label,
  value,
  tone = "plain",
  sub,
  title,
  onClick,
  active = false,
}: {
  label: string;
  value: ReactNode;
  tone?: string;
  /** A quieter second line under the value, e.g. "2 active". */
  sub?: ReactNode;
  title?: string;
  /** Makes the card a button, e.g. one that filters the list below. */
  onClick?: () => void;
  /** The filter this card applies is on. */
  active?: boolean;
}) {
  const className = [
    "stat",
    tone === "plain" ? "" : tone,
    onClick ? "stat-button" : "",
    active ? "active" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const body = (
    <>
      <div className="stat-label">{label}</div>
      <div className="stat-value tnum">{value}</div>
      {sub != null && <div className="stat-sub">{sub}</div>}
    </>
  );
  return onClick ? (
    <button type="button" className={className} title={title} onClick={onClick} aria-pressed={active}>
      {body}
    </button>
  ) : (
    <div className={className} title={title}>
      {body}
    </div>
  );
}

export function Card({
  head,
  children,
  className = "",
}: {
  head?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {head && <div className="card-head">{head}</div>}
      {children}
    </section>
  );
}

export function SectionHead({
  title,
  sub,
  right,
}: {
  title: string;
  sub?: string;
  right?: ReactNode;
}) {
  return (
    <div className="toolbar" style={{ justifyContent: "space-between" }}>
      <div className="section-head">
        <h2 className="section-title">{title}</h2>
        {sub && <div className="section-sub">{sub}</div>}
      </div>
      {right}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p className="muted-note">{children}</p>}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="boot">
      <Loader2 className="spin" size={18} aria-hidden="true" />
      <span>Loading {what}…</span>
    </div>
  );
}

/**
 * A grid table built from CSS grid rather than <table>.
 *
 * Rows need to be buttons (every row in this product drills through), and a
 * <button> is not valid inside <tr>. Grid keeps the columns aligned without
 * fighting table semantics.
 */
export function GridTable({
  columns,
  children,
}: {
  columns: string;
  children: ReactNode;
}) {
  return (
    <div className="grid-table" style={{ ["--cols" as string]: columns }}>
      {children}
    </div>
  );
}

export function GridHead({ columns, cells }: { columns: string; cells: string[] }) {
  return (
    <div className="grid-head" style={{ gridTemplateColumns: columns }}>
      {cells.map((cell) => (
        <div key={cell}>{cell}</div>
      ))}
    </div>
  );
}
