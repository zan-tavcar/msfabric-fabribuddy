/**
 * Shared SVG geometry.
 *
 * Charts are hand-rolled rather than pulled from a library because the mark
 * specs here are exact — 4px rounded data-ends squared at the baseline, 2px
 * surface gaps between touching fills, hairline solid gridlines — and every
 * charting library has to be fought to produce them.
 */

/** Horizontal bar: square at the baseline (left), 4px rounded at the data end. */
export function barPathH(x: number, y: number, w: number, h: number, r = 4) {
  const rr = Math.max(0, Math.min(r, w, h / 2));
  if (w <= 0) return "";
  return [
    `M${x},${y}`,
    `H${x + w - rr}`,
    `Q${x + w},${y} ${x + w},${y + rr}`,
    `V${y + h - rr}`,
    `Q${x + w},${y + h} ${x + w - rr},${y + h}`,
    `H${x}`,
    "Z",
  ].join(" ");
}

/** Vertical column: square at the baseline (bottom), 4px rounded on the cap. */
export function barPathV(x: number, y: number, w: number, h: number, r = 4) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  if (h <= 0) return "";
  return [
    `M${x},${y + h}`,
    `V${y + rr}`,
    `Q${x},${y} ${x + rr},${y}`,
    `H${x + w - rr}`,
    `Q${x + w},${y} ${x + w},${y + rr}`,
    `V${y + h}`,
    "Z",
  ].join(" ");
}

export function linePath(points: [number, number][]) {
  if (points.length === 0) return "";
  return points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x},${y}`).join(" ");
}

export function areaPath(points: [number, number][], baselineY: number) {
  if (points.length === 0) return "";
  const [fx] = points[0];
  const [lx] = points[points.length - 1];
  return `${linePath(points)} L${lx},${baselineY} L${fx},${baselineY} Z`;
}

/** Clean axis ticks: 0 / 500 / 1,000 rather than 0 / 437 / 874. */
export function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const ticks: number[] = [];
  for (let t = 0; t <= max + step * 0.001; t += step) ticks.push(Math.round(t));
  return ticks;
}
