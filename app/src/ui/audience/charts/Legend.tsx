interface Props {
  items: { label: string; color: string }[];
}

/**
 * A legend is always present for two or more series — identity must never
 * depend on color-matching alone. Text wears ink tokens; the swatch beside it
 * carries the series color.
 */
export function Legend({ items }: Props) {
  if (items.length < 2) return null;
  return (
    <ul className="chart-legend">
      {items.map((i) => (
        <li key={i.label}>
          <span className="chart-legend-swatch" style={{ background: i.color }} aria-hidden="true" />
          {i.label}
        </li>
      ))}
    </ul>
  );
}
