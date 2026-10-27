import { num } from "../lib/format";

export interface BarDatum {
  label: string;
  value: number;
  /** Optional secondary line under the label. */
  sub?: string;
}

interface Props {
  data: BarDatum[];
  /** Nominal categories get ONE hue — never a value-ramp, which would just
   *  re-encode bar length as darkness. */
  color?: string;
  onSelect?: (d: BarDatum) => void;
  emptyText?: string;
}

/**
 * Horizontal bars with the value direct-labelled at the tip.
 *
 * The label sits above the bar rather than inside it, so a short bar never
 * clips its own text. The track is a lighter surface, the fill is square at
 * the baseline and 4px-rounded at the data end.
 */
export function BarList({ data, color = "var(--series-1)", onSelect, emptyText }: Props) {
  if (data.length === 0) {
    return <p className="muted-note">{emptyText ?? "Nothing to show."}</p>;
  }
  const max = Math.max(1, ...data.map((d) => d.value));

  return (
    <ul className="barlist">
      {data.map((d) => (
        <li
          key={d.label}
          className={onSelect ? "barlist-row clickable" : "barlist-row"}
          onClick={onSelect ? () => onSelect(d) : undefined}
          {...(onSelect
            ? {
                role: "button",
                tabIndex: 0,
                onKeyDown: (e: React.KeyboardEvent) => {
                  if (e.key === "Enter" || e.key === " ") onSelect(d);
                },
              }
            : {})}
        >
          <div className="barlist-head">
            <span className="barlist-label" title={d.label}>
              {d.label}
            </span>
            <span className="barlist-value">{num(d.value)}</span>
          </div>
          {d.sub && <div className="barlist-sub">{d.sub}</div>}
          <div className="barlist-track">
            <div
              className="barlist-fill"
              style={{
                width: `${Math.max((d.value / max) * 100, d.value > 0 ? 1.5 : 0)}%`,
                background: color,
              }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
