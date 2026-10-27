import { pct } from "../lib/format";

interface Props {
  value: number;
  total: number;
  /** Severity of the FILLED portion. */
  tone?: "accent" | "good" | "warning" | "serious" | "critical";
  label: string;
  caption?: string;
}

const TONE: Record<string, string> = {
  accent: "var(--series-1)",
  good: "var(--good)",
  warning: "var(--warning)",
  serious: "var(--serious)",
  critical: "var(--critical)",
};

/**
 * A single ratio against a limit. The unfilled track is a lighter step of the
 * same ramp rather than plain gray, so state reads across the whole bar.
 * (A two-slice pie would be the wrong form here.)
 */
export function Meter({ value, total, tone = "accent", label, caption }: Props) {
  const share = pct(value, total);
  return (
    <div className="meter">
      <div className="meter-head">
        <span className="meter-label">{label}</span>
        <span className="meter-figure">
          {value.toLocaleString()}
          <span className="meter-total"> / {total.toLocaleString()}</span>
        </span>
      </div>
      <div
        className="meter-track"
        role="meter"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label={label}
      >
        <div
          className="meter-fill"
          style={{ width: `${share}%`, background: TONE[tone] }}
        />
      </div>
      {caption && <p className="meter-caption">{caption}</p>}
    </div>
  );
}
