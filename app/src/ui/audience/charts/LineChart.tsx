import { useState } from "react";
import { useSize } from "../hooks/useSize";
import { areaPath, linePath, niceTicks } from "./geometry";
import { num, shortDate } from "../lib/format";
import { Legend } from "./Legend";

export interface LinePoint {
  date: string;
  value: number;
}

export interface LineSeries {
  key: string;
  label: string;
  color: string;
  data: LinePoint[];
}

/**
 * Trend over time, one or more series on a SINGLE shared axis.
 *
 * Views and viewers are both counts, so they share a scale honestly — a second
 * y-axis would invent a correlation that is not in the data. The area wash is
 * only drawn for the first series, so the lower line stays legible.
 */
export function LineChart({
  series,
  height = 220,
}: {
  series: LineSeries[];
  height?: number;
}) {
  const { ref, width } = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const PAD = { top: 12, right: 14, bottom: 26, left: 48 };
  const plotW = Math.max(0, width - PAD.left - PAD.right);
  const plotH = height - PAD.top - PAD.bottom;

  const len = Math.max(0, ...series.map((s) => s.data.length));
  const max = Math.max(1, ...series.flatMap((s) => s.data.map((d) => d.value)));
  const ticks = niceTicks(max);
  const axisMax = Math.max(max, ticks[ticks.length - 1] ?? max);

  const stepX = len > 1 ? plotW / (len - 1) : 0;
  const xOf = (i: number) => PAD.left + i * stepX;
  const yOf = (v: number) => PAD.top + plotH - (v / axisMax) * plotH;
  const dates = series[0]?.data.map((d) => d.date) ?? [];

  return (
    <div className="chart" ref={ref}>
      {width > 0 && len > 0 && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={series.map((s) => s.label).join(" and ")}
          onMouseLeave={() => setHover(null)}
          onMouseMove={(e) => {
            const box = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            const i = Math.round((e.clientX - box.left - PAD.left) / (stepX || 1));
            setHover(Math.max(0, Math.min(len - 1, i)));
          }}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={PAD.left}
                x2={PAD.left + plotW}
                y1={yOf(t)}
                y2={yOf(t)}
                stroke="var(--gridline)"
                strokeWidth={1}
              />
              <text x={PAD.left - 8} y={yOf(t) + 4} textAnchor="end" className="axis-text">
                {num(t)}
              </text>
            </g>
          ))}

          {series.map((s, si) => {
            const pts: [number, number][] = s.data.map((d, i) => [xOf(i), yOf(d.value)]);
            if (pts.length === 0) return null;
            return (
              <g key={s.key}>
                {si === 0 && (
                  <path d={areaPath(pts, PAD.top + plotH)} fill={s.color} opacity={0.1} />
                )}
                <path
                  d={linePath(pts)}
                  fill="none"
                  stroke={s.color}
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <circle
                  cx={pts[pts.length - 1][0]}
                  cy={pts[pts.length - 1][1]}
                  r={4}
                  fill={s.color}
                  stroke="var(--surface)"
                  strokeWidth={2}
                />
              </g>
            );
          })}

          {hover !== null && (
            <>
              <line
                x1={xOf(hover)}
                x2={xOf(hover)}
                y1={PAD.top}
                y2={PAD.top + plotH}
                stroke="var(--baseline)"
                strokeWidth={1}
              />
              {series.map((s) => {
                const d = s.data[hover];
                if (!d) return null;
                return (
                  <circle
                    key={s.key}
                    cx={xOf(hover)}
                    cy={yOf(d.value)}
                    r={5}
                    fill={s.color}
                    stroke="var(--surface)"
                    strokeWidth={2}
                  />
                );
              })}
            </>
          )}

          <line
            x1={PAD.left}
            x2={PAD.left + plotW}
            y1={PAD.top + plotH}
            y2={PAD.top + plotH}
            stroke="var(--baseline)"
            strokeWidth={1}
          />
          {dates.length > 0 && (
            <>
              <text x={PAD.left} y={height - 8} className="axis-text">
                {shortDate(dates[0])}
              </text>
              <text x={PAD.left + plotW} y={height - 8} textAnchor="end" className="axis-text">
                {shortDate(dates[dates.length - 1])}
              </text>
            </>
          )}
        </svg>
      )}

      {hover !== null && dates[hover] && (
        <div
          className="tooltip"
          style={{ left: `${Math.min(Math.max(xOf(hover) - 80, 8), Math.max(8, width - 210))}px` }}
        >
          <div className="tooltip-title">{shortDate(dates[hover])}</div>
          {series.map((s) => (
            <div key={s.key} className="tooltip-row">
              <span className="swatch" style={{ background: s.color }} />
              <span className="tooltip-label">{s.label}</span>
              <span className="tooltip-value">{num(s.data[hover]?.value ?? 0)}</span>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <Legend items={series.map((s) => ({ label: s.label, color: s.color }))} />
      </div>
    </div>
  );
}
